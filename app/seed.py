"""Load DEMO data so the system can be tried before CFC's real recipe sheet is imported.

    python -m app.seed          # adds demo staff, ingredients and menu
    python -m app.seed --reset  # wipes the database first

All prices, costs and quantities here are placeholders, not CFC's real numbers.
"""
import os
import sys

from .db import DB_PATH, init_db, tx

STAFF = [("Admin", "1234", "admin"), ("Manager Demo", "2222", "manager"), ("Cashier Demo", "1111", "cashier")]

# name, unit, cost per unit (KES), par level, count daily
INGREDIENTS = [
    ("Basmati rice", "g", 0.25, 10000, 1),
    ("Chicken (raw)", "g", 0.55, 8000, 1),
    ("Goat meat (raw)", "g", 0.85, 6000, 1),
    ("Beef (raw)", "g", 0.70, 4000, 1),
    ("Cooking oil", "ml", 0.30, 5000, 1),
    ("Onions", "g", 0.12, 3000, 0),
    ("Tomatoes", "g", 0.10, 3000, 0),
    ("Potatoes", "g", 0.08, 8000, 1),
    ("Mandi spice mix", "g", 1.20, 500, 0),
    ("Soda 500ml", "pcs", 45, 24, 1),
    ("Water 500ml", "pcs", 25, 24, 1),
    ("Passion fruit", "g", 0.20, 2000, 0),
    ("Sugar", "g", 0.15, 2000, 0),
]

# menu item, category, price, [(ingredient, qty per portion)]
MENU = [
    ("Chicken Mandi", "Mandi", 950, [("Basmati rice", 250), ("Chicken (raw)", 450), ("Cooking oil", 30),
                                      ("Onions", 40), ("Mandi spice mix", 8)]),
    ("Goat Mandi", "Mandi", 1200, [("Basmati rice", 250), ("Goat meat (raw)", 400), ("Cooking oil", 30),
                                    ("Onions", 40), ("Mandi spice mix", 8)]),
    ("Beef Pilau", "Rice", 650, [("Basmati rice", 200), ("Beef (raw)", 200), ("Cooking oil", 25),
                                  ("Onions", 50), ("Tomatoes", 50)]),
    ("Chips (regular)", "Sides", 250, [("Potatoes", 300), ("Cooking oil", 40)]),
    ("Kachumbari", "Sides", 100, [("Tomatoes", 80), ("Onions", 40)]),
    ("Soda 500ml", "Drinks", 100, [("Soda 500ml", 1)]),
    ("Water 500ml", "Drinks", 80, [("Water 500ml", 1)]),
    ("Fresh passion juice", "Drinks", 200, [("Passion fruit", 120), ("Sugar", 25)]),
]


def seed(reset=False):
    if reset and os.path.exists(DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(DB_PATH + suffix):
                os.remove(DB_PATH + suffix)
    init_db()
    with tx() as c:
        for name, pin, role in STAFF:
            c.execute("INSERT OR IGNORE INTO staff(name, pin, role) VALUES (?, ?, ?)", (name, pin, role))
        for row in INGREDIENTS:
            c.execute(
                "INSERT OR IGNORE INTO ingredients(name, unit, cost_per_unit, par_level, count_daily) VALUES (?,?,?,?,?)",
                row,
            )
        for sort, cat in enumerate(dict.fromkeys(m[1] for m in MENU)):
            c.execute("INSERT OR IGNORE INTO categories(name, sort) VALUES (?, ?)", (cat, sort))
        for name, cat, price, recipe in MENU:
            cat_id = c.execute("SELECT id FROM categories WHERE name = ?", (cat,)).fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO menu_items(name, category_id, price) VALUES (?, ?, ?)", (name, cat_id, price))
            mid = c.execute("SELECT id FROM menu_items WHERE name = ?", (name,)).fetchone()["id"]
            for ing, qty in recipe:
                iid = c.execute("SELECT id FROM ingredients WHERE name = ?", (ing,)).fetchone()["id"]
                c.execute("INSERT OR IGNORE INTO recipe_lines(menu_item_id, ingredient_id, qty) VALUES (?, ?, ?)",
                          (mid, iid, qty))
    print("Demo data loaded. PINs: admin 1234, manager 2222, cashier 1111")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
