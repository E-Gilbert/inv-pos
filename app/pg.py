"""Postgres backend for cloud mode (Render + Supabase).

The app's SQL was written for SQLite. Rather than fork every query, this module gives
Postgres the same small surface the app uses from sqlite3 — `conn.execute(sql, params)`,
`fetchone()`, `fetchall()`, iteration, `lastrowid`, rows readable by name or position — and
translates the few SQLite-only bits of SQL on the way through:

    ?                          -> %s
    INSERT OR IGNORE INTO ...  -> INSERT INTO ... ON CONFLICT DO NOTHING
    GROUP_CONCAT(x, sep)       -> STRING_AGG((x)::text, sep)
    COLLATE NOCASE             -> dropped (those columns are CITEXT in schema_pg.sql)
    BEGIN IMMEDIATE            -> BEGIN + one transaction-scoped advisory lock
    INSERT into an id table    -> ... RETURNING id   (gives cursor.lastrowid)

Concurrency: SQLite's BEGIN IMMEDIATE lets one writer in at a time, and the business rules
(receipt numbers, stock figures, one open day) rely on that. The advisory lock gives Postgres
exactly the same guarantee. One restaurant's traffic is far below what that costs.

Connections come from a small pool. Use Supabase's *session* pooler URL (or a direct
connection): the time zone and search_path are set once per connection.
"""
import atexit
import os
import re
from functools import lru_cache

import psycopg
from psycopg.types.numeric import FloatLoader
from psycopg_pool import ConnectionPool

SCHEMA_VERSION = 5
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_pg.sql")
WRITE_LOCK_KEY = 0x1A7_905  # arbitrary, constant: "INV-POS write lock"

# Tables whose primary key is an integer `id` (INSERTs into these get RETURNING id).
ID_TABLES = frozenset({
    "staff", "suppliers", "ingredients", "categories", "menu_items", "recipe_lines", "import_batches",
    "days", "shifts", "orders", "order_items", "purchase_orders", "po_lines", "refunds", "refund_lines",
    "stock_movements", "audit_log", "licence", "tabs", "tab_lines",
})


class Row:
    """Read-only row like sqlite3.Row: row["name"], row[0], dict(row), iteration over values."""
    __slots__ = ("_keys", "_vals", "_index")

    def __init__(self, keys, vals, index):
        self._keys, self._vals, self._index = keys, vals, index

    def __getitem__(self, k):
        if isinstance(k, (int, slice)):
            return self._vals[k]
        return self._vals[self._index[k]]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __eq__(self, other):
        return tuple(self) == tuple(other)

    def __hash__(self):
        return hash(tuple(self._vals))

    def __repr__(self):
        return f"Row({dict(zip(self._keys, self._vals, strict=True))!r})"


def _row_factory(cursor):
    keys = [d.name for d in (cursor.description or [])]
    index = {k: i for i, k in enumerate(keys)}
    return lambda values: Row(keys, values, index)


# ---------------------------------------------------------------- SQL translation

_INSERT_TABLE = re.compile(r"^\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(\w+)", re.I)
_OR_IGNORE = re.compile(r"^(\s*INSERT)\s+OR\s+IGNORE\s+(INTO)", re.I)
_NOCASE = re.compile(r"\s+COLLATE\s+NOCASE\b", re.I)
_GROUP_CONCAT = re.compile(r"\bGROUP_CONCAT\s*\(", re.I)


def _split_top_level_args(s, start):
    """From the char after '(' at `start`, return (args, end) where s[end] is the matching ')'."""
    depth, quote, args, cur = 0, False, [], []
    i = start
    while i < len(s):
        ch = s[i]
        if quote:
            cur.append(ch)
            if ch == "'":
                quote = False
        elif ch == "'":
            quote = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    raise ValueError("Unbalanced parentheses in GROUP_CONCAT")


def _group_concat(sql):
    while True:
        m = _GROUP_CONCAT.search(sql)
        if not m:
            return sql
        args, end = _split_top_level_args(sql, m.end())
        sep = args[1] if len(args) > 1 else "','"
        sql = f"{sql[:m.start()]}STRING_AGG(({args[0]})::text, {sep}){sql[end + 1:]}"


def _placeholders(sql):
    """? -> %s outside string literals; literal % -> %% everywhere (psycopg parses the whole text)."""
    out, quote = [], False
    for ch in sql:
        if ch == "%":
            out.append("%%")
        elif quote:
            out.append(ch)
            if ch == "'":
                quote = False
        elif ch == "'":
            quote = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


@lru_cache(maxsize=2048)
def translate(sql):
    """Returns (postgres_sql, returns_id)."""
    s = sql.strip().rstrip(";")
    ignore = bool(_OR_IGNORE.match(s))
    if ignore:
        s = _OR_IGNORE.sub(r"\1 \2", s, count=1)
    s = _NOCASE.sub("", s)
    s = _group_concat(s)
    m = _INSERT_TABLE.match(s)
    returns_id = False
    if m:
        if ignore and not re.search(r"\bON\s+CONFLICT\b", s, re.I):
            s += " ON CONFLICT DO NOTHING"
        if m.group(1).lower() in ID_TABLES and not re.search(r"\bRETURNING\b", s, re.I):
            s += " RETURNING id"
            returns_id = True
    return _placeholders(s), returns_id


# ---------------------------------------------------------------- connection wrapper

class Cursor:
    def __init__(self, rows, lastrowid=None):
        self._rows = rows
        self._i = 0
        self.lastrowid = lastrowid
        self.rowcount = len(rows)

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rest = self._rows[self._i:]
        self._i = len(self._rows)
        return rest

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r


class Connection:
    """Wraps one pooled psycopg connection (autocommit; transactions are explicit BEGIN/COMMIT)."""

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    @property
    def in_transaction(self):
        return self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def execute(self, sql, params=()):
        head = sql.strip().upper()
        if head == "BEGIN IMMEDIATE":
            self._conn.execute("BEGIN")
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
            return Cursor([])
        if head in ("BEGIN", "COMMIT", "ROLLBACK"):
            self._conn.execute(head)
            return Cursor([])
        q, returns_id = translate(sql)
        cur = self._conn.execute(q, tuple(params) if params is not None else ())
        rows = cur.fetchall() if cur.description else []
        lastrowid = None
        if returns_id:
            lastrowid = rows[0][0] if rows else None
            rows = []
        return Cursor(rows, lastrowid)

    def close(self):
        if self._conn is None:
            return
        if self.in_transaction:
            self._conn.execute("ROLLBACK")
        self._pool.putconn(self._conn)
        self._conn = None


# ---------------------------------------------------------------- pool / schema

_pool = None


def _valid_ident(name):
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"Invalid Postgres schema name: {name!r}")
    return name


def pg_schema():
    return _valid_ident(os.environ.get("INVPOS_PG_SCHEMA", "inventory").strip().lower() or "inventory")


def _configure(conn):
    conn.adapters.register_loader("numeric", FloatLoader)  # AVG()/SUM() come back as float, not Decimal
    conn.execute("SELECT set_config('TimeZone', %s, false)", (os.environ.get("TZ") or "Africa/Nairobi",))
    sch = pg_schema()
    conn.execute(f"SET search_path TO {sch}, public, extensions")  # nosec B608


def pool(url):
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            url, min_size=1, max_size=int(os.environ.get("INVPOS_PG_POOL", "5")), open=True,
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": _row_factory},
            configure=_configure, check=ConnectionPool.check_connection, timeout=15)
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


atexit.register(close_pool)


def connect(url):
    return Connection(pool(url))


MIGRATIONS = {}  # {6: callable(conn)} — future Postgres migrations go here


def init_db(url, default_settings):
    conn = connect(url)
    try:
        sch = pg_schema()
        # Only create the schema when missing: a restricted role (Supabase setup in docs/CLOUD.md)
        # owns an existing schema but has no CREATE right on the database itself.
        if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = ?", (sch,)).fetchone():
            conn._conn.execute(f"CREATE SCHEMA {sch}")  # nosec B608
        conn.execute("BEGIN IMMEDIATE")
        fresh = not conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = 'schema_meta'",
            (sch,)).fetchone()
        if fresh:
            with open(SCHEMA_FILE, encoding="utf-8") as f:
                conn._conn.execute(f.read())
            conn.execute("INSERT INTO schema_meta(id, version) VALUES (1, ?)", (SCHEMA_VERSION,))
        version = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()[0]
        for v in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[v](conn)
            conn.execute("UPDATE schema_meta SET version = ? WHERE id = 1", (v,))
        for k, v in default_settings.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        conn.execute("COMMIT")
    finally:
        conn.close()


def wipe(url):
    """Drop every INV-POS table in the configured schema (demo/test reset only)."""
    conn = connect(url)
    try:
        sch = pg_schema()
        if sch == "public":
            tables = [r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")]
            for t in ("schema_meta", *sorted(ID_TABLES), "settings", "sessions", "day_stock", "terminals",
                      "licence_revocations"):
                if t in tables:
                    conn._conn.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")  # nosec B608
        else:
            conn._conn.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")  # nosec B608
    finally:
        conn.close()

