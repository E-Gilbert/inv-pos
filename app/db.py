"""SQLite database: connection, schema and migrations.

One file (data/cfc.db) holds everything. SQLite is fine for a single restaurant on a LAN:
a few tills writing a few orders a minute is nothing for it. WAL mode lets report screens
read while tills write.

Transactions: every request runs inside `tx()`, which starts with BEGIN IMMEDIATE. That takes
the write lock up front, so two tills can never read the same "next receipt number" or the
same stock figure and then both write — writers simply queue (busy_timeout 10 s).

Migrations: the schema version is kept in PRAGMA user_version. Before any migration runs
on an existing database, a full copy is saved to data/backups/pre-migration-*.db.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("CFC_DB", os.path.join(BASE_DIR, "data", "cfc.db"))
DATA_DIR = os.path.dirname(DB_PATH)
SCHEMA_VERSION = 3

# ---------------------------------------------------------------- v1 (original v0.1 schema)
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, pin TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('cashier','manager','admin')), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, staff_id INTEGER NOT NULL REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    unit TEXT NOT NULL CHECK (unit IN ('g','ml','pcs')), cost_per_unit REAL NOT NULL DEFAULT 0,
    par_level REAL NOT NULL DEFAULT 0, count_daily INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category_id INTEGER REFERENCES categories(id), price REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS recipe_lines (
    id INTEGER PRIMARY KEY, menu_item_id INTEGER NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), qty REAL NOT NULL CHECK (qty > 0),
    UNIQUE (menu_item_id, ingredient_id));
CREATE TABLE IF NOT EXISTS days (
    id INTEGER PRIMARY KEY, business_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('open','closed')),
    opened_by INTEGER REFERENCES staff(id), opened_at TEXT,
    closed_by INTEGER REFERENCES staff(id), closed_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, day_id INTEGER NOT NULL REFERENCES days(id), number INTEGER NOT NULL,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), total REAL NOT NULL,
    payment_method TEXT NOT NULL CHECK (payment_method IN ('cash','mpesa','card')),
    amount_tendered REAL, payment_ref TEXT,
    status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid','void')),
    void_reason TEXT, voided_by INTEGER REFERENCES staff(id), UNIQUE (day_id, number));
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id), name TEXT NOT NULL,
    price REAL NOT NULL, qty INTEGER NOT NULL CHECK (qty > 0));
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY, day_id INTEGER NOT NULL REFERENCES days(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    type TEXT NOT NULL CHECK (type IN ('delivery','sale','void','waste')), qty REAL NOT NULL,
    order_id INTEGER REFERENCES orders(id), reason TEXT, note TEXT, staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS ix_mov_day ON stock_movements(day_id, ingredient_id);
CREATE TABLE IF NOT EXISTS day_stock (
    day_id INTEGER NOT NULL REFERENCES days(id), ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    opening REAL NOT NULL DEFAULT 0, delivered REAL, sold REAL, wasted REAL, expected REAL,
    counted REAL, variance REAL, reason TEXT, note TEXT, PRIMARY KEY (day_id, ingredient_id));
"""

# ---------------------------------------------------------------- v2
# Stock movement types (qty is signed: + adds stock, - removes it):
#   delivery       +  goods physically received (direct or against a purchase order)
#   sale           -  written automatically when an order is paid (recipe snapshot)
#   void           +  reverses a whole sale (same day)
#   refund         +  returned items put back on the shelf (only when restocked)
#   waste          -  discarded / spoiled / staff meal
#   adjustment     +/-  manager correction with a reason (found stock, wrong delivery qty...)
#   external_sale  -  sales recorded by another POS (imported workbook history)
MOVEMENT_TYPES = ("delivery", "sale", "void", "refund", "waste", "adjustment", "external_sale")

V2_NEW_TABLES = """
CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, phone TEXT, email TEXT,
    notes TEXT, active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE purchase_orders (
    id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    status TEXT NOT NULL CHECK (status IN ('draft','ordered','partial','received','cancelled')),
    expected_date TEXT, notes TEXT,
    created_by INTEGER REFERENCES staff(id), created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ordered_at TEXT, closed_at TEXT);

CREATE TABLE po_lines (
    id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    qty_ordered REAL NOT NULL CHECK (qty_ordered > 0),
    qty_received REAL NOT NULL DEFAULT 0 CHECK (qty_received >= 0),
    unit_cost REAL NOT NULL DEFAULT 0 CHECK (unit_cost >= 0),
    UNIQUE (po_id, ingredient_id));

CREATE TABLE shifts (
    id INTEGER PRIMARY KEY, staff_id INTEGER NOT NULL REFERENCES staff(id), day_id INTEGER NOT NULL REFERENCES days(id),
    status TEXT NOT NULL CHECK (status IN ('open','closed')),
    opening_float REAL NOT NULL DEFAULT 0 CHECK (opening_float >= 0),
    opened_at TEXT NOT NULL, closed_at TEXT, closed_by INTEGER REFERENCES staff(id),
    expected_cash REAL, counted_cash REAL, cash_variance REAL, notes TEXT);
CREATE UNIQUE INDEX ux_shift_open ON shifts(staff_id) WHERE status = 'open';

CREATE TABLE refunds (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
    day_id INTEGER NOT NULL REFERENCES days(id), shift_id INTEGER REFERENCES shifts(id),
    amount REAL NOT NULL CHECK (amount >= 0), tax REAL NOT NULL DEFAULT 0,
    method TEXT NOT NULL CHECK (method IN ('cash','mpesa','card')), restocked INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL, staff_id INTEGER NOT NULL REFERENCES staff(id), approved_by INTEGER REFERENCES staff(id),
    client_ref TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE UNIQUE INDEX ux_refund_ref ON refunds(client_ref) WHERE client_ref IS NOT NULL;

CREATE TABLE refund_lines (
    id INTEGER PRIMARY KEY, refund_id INTEGER NOT NULL REFERENCES refunds(id) ON DELETE CASCADE,
    order_item_id INTEGER NOT NULL REFERENCES order_items(id), qty INTEGER NOT NULL CHECK (qty > 0),
    amount REAL NOT NULL);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    staff_id INTEGER REFERENCES staff(id), action TEXT NOT NULL, entity TEXT, entity_id INTEGER,
    details TEXT, ip TEXT);
CREATE INDEX ix_audit_at ON audit_log(at);

CREATE TABLE import_batches (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, filename TEXT, sha256 TEXT NOT NULL,
    staff_id INTEGER REFERENCES staff(id), created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    summary TEXT, UNIQUE (kind, sha256));

CREATE TABLE terminals (
    id TEXT PRIMARY KEY, label TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    user_agent TEXT, active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE licence (
    id INTEGER PRIMARY KEY CHECK (id = 1), token TEXT NOT NULL, installed_at TEXT NOT NULL,
    installed_by INTEGER REFERENCES staff(id), last_valid_at TEXT);
CREATE TABLE licence_revocations (licence_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL, list_issued TEXT);
"""

DEFAULT_SETTINGS = {
    "business_name": "City Food Court",
    "business_address": "Kisumu",
    "business_pin": "",                      # KRA PIN printed on receipts when set
    "receipt_footer": "Thank you! Karibu tena.",
    "currency": "KES",
    # A variance smaller than this % of what was available (opening + delivered)
    # is treated as normal measuring noise and needs no explanation.
    "variance_tolerance_pct": "2",
    # Tax: off until the business confirms its VAT position. Prices are entered tax-inclusive.
    "tax_rate_pct": "0",
    "tax_inclusive": "1",
    "tax_label": "VAT",
    # Discounts above this % need a manager's PIN (0 = every discount needs approval).
    "cashier_max_discount_pct": "0",
    # Stock status: if an ingredient has no critical level of its own, critical = this % of its reorder level.
    "critical_pct_of_reorder": "50",
    # Reorder suggestions cover this many days of average usage on top of the reorder level.
    "reorder_cover_days": "3",
    # manual | last_purchase | weighted_average
    "cost_method": "manual",
    "require_shift": "1",
    "session_idle_minutes": "720",
    # Reporting (MICROS-style): order types offered at checkout, whether to ask for a guest count,
    # and serving periods as "Name=HH:MM-HH:MM;..." (a period may run past midnight).
    "order_types": "Dine in,Takeaway,Delivery",
    "ask_guests": "0",
    "serving_periods": "Breakfast=05:00-11:00;Lunch=11:00-16:00;Dinner=16:00-05:00",
}


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # we manage transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def tx():
    """One connection inside one IMMEDIATE transaction: commits on success, rolls back on any error."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _backup_before_migration(version):
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return None
    out = os.path.join(DATA_DIR, "backups")
    os.makedirs(out, exist_ok=True)
    target = os.path.join(out, f"pre-migration-v{version}-{datetime.now():%Y%m%d-%H%M%S}.db")
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def _migrate_to_2(c):
    from .security import hash_pin, weak_pin  # local import: security imports this module

    # staff: hash PINs, add stock_controller role, flag weak manager/admin PINs for a forced change.
    rows = c.execute("SELECT * FROM staff").fetchall()
    c.execute("""CREATE TABLE staff_new (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, pin_hash TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK (role IN ('cashier','stock_controller','manager','admin')),
        active INTEGER NOT NULL DEFAULT 1, must_change_pin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
    for r in rows:
        must = 1 if (r["role"] in ("manager", "admin") and weak_pin(r["pin"])) else 0
        c.execute("INSERT INTO staff_new(id, name, pin_hash, role, active, must_change_pin) VALUES (?,?,?,?,?,?)",
                  (r["id"], r["name"], hash_pin(r["pin"]), r["role"], r["active"], must))
    c.execute("DROP TABLE staff")
    c.execute("ALTER TABLE staff_new RENAME TO staff")

    # sessions: tokens are now stored hashed, with expiry. Old sessions are dropped (staff log in again).
    c.execute("DROP TABLE sessions")
    c.execute("""CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY, staff_id INTEGER NOT NULL REFERENCES staff(id),
        created_at TEXT NOT NULL, last_seen TEXT NOT NULL, expires_at TEXT NOT NULL,
        ip TEXT, terminal_id TEXT)""")
    c.execute("CREATE INDEX ix_sessions_staff ON sessions(staff_id)")

    for stmt in [s.strip() for s in V2_NEW_TABLES.split(";") if s.strip()]:
        c.execute(stmt)

    # ingredients / menu: identifiers, thresholds, supplier.
    for col, ddl in [("sku", "TEXT"), ("barcode", "TEXT"), ("category", "TEXT"),
                     ("critical_level", "REAL NOT NULL DEFAULT 0"),
                     ("supplier_id", "INTEGER REFERENCES suppliers(id)")]:
        if col not in _cols(c, "ingredients"):
            c.execute(f"ALTER TABLE ingredients ADD COLUMN {col} {ddl}")
    for col in ("sku", "barcode"):
        if col not in _cols(c, "menu_items"):
            c.execute(f"ALTER TABLE menu_items ADD COLUMN {col} TEXT")
    c.execute("CREATE UNIQUE INDEX ux_ing_sku ON ingredients(sku) WHERE sku IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX ux_ing_barcode ON ingredients(barcode) WHERE barcode IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX ux_menu_sku ON menu_items(sku) WHERE sku IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX ux_menu_barcode ON menu_items(barcode) WHERE barcode IS NOT NULL")

    # orders: idempotency key, discount, tax, shift, payment status, refunded total.
    for col, ddl in [("client_ref", "TEXT"), ("request_hash", "TEXT"), ("subtotal", "REAL"),
                     ("discount", "REAL NOT NULL DEFAULT 0"), ("discount_reason", "TEXT"),
                     ("discount_by", "INTEGER REFERENCES staff(id)"),
                     ("tax", "REAL NOT NULL DEFAULT 0"), ("tax_rate", "REAL NOT NULL DEFAULT 0"),
                     ("tax_inclusive", "INTEGER NOT NULL DEFAULT 1"),
                     ("shift_id", "INTEGER REFERENCES shifts(id)"),
                     ("payment_status", "TEXT NOT NULL DEFAULT 'manual_confirmed'"),
                     ("refunded", "REAL NOT NULL DEFAULT 0"), ("terminal_id", "TEXT")]:
        if col not in _cols(c, "orders"):
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
    c.execute("UPDATE orders SET subtotal = total WHERE subtotal IS NULL")
    c.execute("CREATE UNIQUE INDEX ux_orders_client_ref ON orders(client_ref) WHERE client_ref IS NOT NULL")
    c.execute("CREATE INDEX ix_orders_payref ON orders(payment_method, payment_ref)")
    c.execute("CREATE INDEX ix_orders_day ON orders(day_id, status)")

    # stock_movements: rebuild to widen the type list and add cost / link columns.
    c.execute(f"""CREATE TABLE stock_movements_new (
        id INTEGER PRIMARY KEY, day_id INTEGER NOT NULL REFERENCES days(id),
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        type TEXT NOT NULL CHECK (type IN ({",".join(repr(t) for t in MOVEMENT_TYPES)})),
        qty REAL NOT NULL, unit_cost REAL,
        order_id INTEGER REFERENCES orders(id), order_item_id INTEGER REFERENCES order_items(id),
        po_line_id INTEGER REFERENCES po_lines(id), refund_id INTEGER REFERENCES refunds(id),
        reason TEXT, note TEXT, staff_id INTEGER REFERENCES staff(id), client_ref TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
    c.execute("""INSERT INTO stock_movements_new(id, day_id, ingredient_id, type, qty, order_id, reason, note, staff_id, created_at)
                 SELECT id, day_id, ingredient_id, type, qty, order_id, reason, note, staff_id, created_at FROM stock_movements""")
    c.execute("DROP TABLE stock_movements")
    c.execute("ALTER TABLE stock_movements_new RENAME TO stock_movements")
    c.execute("CREATE INDEX ix_mov_day ON stock_movements(day_id, ingredient_id)")
    c.execute("CREATE INDEX ix_mov_order ON stock_movements(order_id)")
    c.execute("CREATE INDEX ix_mov_ing ON stock_movements(ingredient_id, created_at)")
    c.execute("CREATE UNIQUE INDEX ux_mov_client_ref ON stock_movements(client_ref) WHERE client_ref IS NOT NULL")

    if "adjusted" not in _cols(c, "day_stock"):
        c.execute("ALTER TABLE day_stock ADD COLUMN adjusted REAL")
    for col, ddl in [("source", "TEXT NOT NULL DEFAULT 'app'"), ("covers_to", "TEXT"),
                     ("import_batch_id", "INTEGER REFERENCES import_batches(id)")]:
        if col not in _cols(c, "days"):
            c.execute(f"ALTER TABLE days ADD COLUMN {col} {ddl}")
    c.execute("CREATE UNIQUE INDEX ux_one_open_day ON days(status) WHERE status = 'open'")


def _migrate_to_3(c):
    """Reporting dimensions: order type and guest count per check, major group per category."""
    if "order_type" not in _cols(c, "orders"):
        c.execute("ALTER TABLE orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'Dine in'")
    if "guests" not in _cols(c, "orders"):
        c.execute("ALTER TABLE orders ADD COLUMN guests INTEGER CHECK (guests IS NULL OR guests BETWEEN 0 AND 500)")
    if "major_group" not in _cols(c, "categories"):
        c.execute("ALTER TABLE categories ADD COLUMN major_group TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_refunds_day ON refunds(day_id)")


MIGRATIONS = {2: _migrate_to_2, 3: _migrate_to_3}


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        fresh = not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='staff'").fetchone()
        if version < SCHEMA_VERSION and not fresh:
            path = _backup_before_migration(version)
            if path:
                print(f"Database backed up before upgrade: {path}")
        conn.executescript(SCHEMA_V1)  # no-op on an existing database
        conn.execute("PRAGMA foreign_keys = OFF")  # required while tables are rebuilt
        for v in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[v](conn)
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError(f"Migration to v{v} left broken references: {[tuple(p) for p in problems[:5]]}")
                conn.execute(f"PRAGMA user_version = {v}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        conn.execute("COMMIT")
    finally:
        conn.close()


def get_setting(c, key):
    row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def setting_float(c, key):
    try:
        return float(get_setting(c, key))
    except (TypeError, ValueError):
        return float(DEFAULT_SETTINGS[key])


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def audit(c, staff_id, action, entity=None, entity_id=None, details=None, ip=None):
    c.execute(
        "INSERT INTO audit_log(at, staff_id, action, entity, entity_id, details, ip) VALUES (?,?,?,?,?,?,?)",
        (now(), staff_id, action, entity, entity_id,
         json.dumps(details, default=str, ensure_ascii=False) if details is not None else None, ip),
    )
