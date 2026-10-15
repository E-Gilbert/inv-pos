"""SQLite database: connection + schema.

One file (data/cfc.db) holds everything. SQLite is fine for a single restaurant
on a LAN: a few tills writing a few orders a minute is nothing for it.
WAL mode lets the report screens read while tills are writing.
"""
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("CFC_DB", os.path.join(BASE_DIR, "data", "cfc.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    pin TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('cashier','manager','admin')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Raw materials. qty is always stored in the base unit (g, ml or pcs).
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    unit TEXT NOT NULL CHECK (unit IN ('g','ml','pcs')),
    cost_per_unit REAL NOT NULL DEFAULT 0,      -- KES per base unit
    par_level REAL NOT NULL DEFAULT 0,          -- reorder warning level
    count_daily INTEGER NOT NULL DEFAULT 1,     -- must be physically counted at close
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category_id INTEGER REFERENCES categories(id),
    price REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

-- Bill of materials: how much of each ingredient ONE portion uses.
CREATE TABLE IF NOT EXISTS recipe_lines (
    id INTEGER PRIMARY KEY,
    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    qty REAL NOT NULL CHECK (qty > 0),
    UNIQUE (menu_item_id, ingredient_id)
);

-- A business day. Only one can be open at a time.
CREATE TABLE IF NOT EXISTS days (
    id INTEGER PRIMARY KEY,
    business_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('open','closed')),
    opened_by INTEGER REFERENCES staff(id),
    opened_at TEXT,
    closed_by INTEGER REFERENCES staff(id),
    closed_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    day_id INTEGER NOT NULL REFERENCES days(id),
    number INTEGER NOT NULL,                    -- receipt number within the day
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    total REAL NOT NULL,
    payment_method TEXT NOT NULL CHECK (payment_method IN ('cash','mpesa','card')),
    amount_tendered REAL,
    payment_ref TEXT,
    status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid','void')),
    void_reason TEXT,
    voided_by INTEGER REFERENCES staff(id),
    UNIQUE (day_id, number)
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id),
    name TEXT NOT NULL,     -- snapshot, so old receipts don't change if the menu does
    price REAL NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0)
);

-- Every change to stock is a row here. Nothing is ever overwritten,
-- so any number on a report can be traced back to who did what and when.
--   delivery  +  goods received
--   sale      -  written automatically when an order is paid (recipe snapshot)
--   void      +  reverses a sale
--   waste     -  discarded / spoiled / staff meal, recorded during the day
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY,
    day_id INTEGER NOT NULL REFERENCES days(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    type TEXT NOT NULL CHECK (type IN ('delivery','sale','void','waste')),
    qty REAL NOT NULL,                          -- signed: + adds stock, - removes it
    order_id INTEGER REFERENCES orders(id),
    reason TEXT,
    note TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_mov_day ON stock_movements(day_id, ingredient_id);

-- Opening and closing counts, one row per ingredient per day.
-- At close the system freezes expected vs counted here, with the reason.
CREATE TABLE IF NOT EXISTS day_stock (
    day_id INTEGER NOT NULL REFERENCES days(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    opening REAL NOT NULL DEFAULT 0,
    delivered REAL,
    sold REAL,
    wasted REAL,
    expected REAL,
    counted REAL,
    variance REAL,
    reason TEXT,
    note TEXT,
    PRIMARY KEY (day_id, ingredient_id)
);
"""

DEFAULT_SETTINGS = {
    "business_name": "City Food Court",
    "receipt_footer": "Thank you! Karibu tena.",
    # A variance smaller than this % of what was available (opening + delivered)
    # is treated as normal measuring noise and needs no explanation.
    "variance_tolerance_pct": "2",
}


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    """A connection inside one transaction: commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with tx() as c:
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_setting(c, key):
    row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)
