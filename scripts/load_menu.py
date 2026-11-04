"""Load a menu catalogue CSV into the POS (safe to run again: unchanged items are left alone).

    python scripts/load_menu.py                              # menus/cfc_menu.csv
    python scripts/load_menu.py menus/cfc_menu.csv --hide-missing --cfc
    python scripts/load_menu.py --preview                    # check only, change nothing

--hide-missing  hide (never delete) active items that are not in the file, e.g. the demo menu
--cfc           also apply City Food Court's shop profile:
                  * VAT 16 % INCLUDED in menu prices (the menu says "Prices ... inclusive of VAT"):
                    the till charges exactly the menu price and only shows the VAT portion on the receipt
                  * business name/address/phone, receipt footer, order types incl. Glovo and Uber Eats
                  * resale items linked to their stock items 1:1 (sodas, bottled water, chapati, samosas,
                    mandazi) using the stock names from CFC's daily stock-movement workbook
The same menu import is available in Setup → Import / export.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import importer  # noqa: E402
from app.db import BASE_DIR, audit, init_db, tx  # noqa: E402

CFC_SETTINGS = {
    "business_name": "City Food Court – House of Mandi",
    "business_address": "Kisumu",
    "business_phone": "+254 707 530 530",
    "receipt_footer": "Thank you for dining with us. Halal food. Order online: cityfoodcourt.co.ke",
    "tax_rate_pct": "16",
    "tax_inclusive": "1",
    "tax_label": "VAT",
    "order_types": "Dine in,Takeaway,Delivery,Glovo,Uber Eats",
}

# menu item -> (stock item as named in the workbook, quantity per item sold). Only obvious 1:1 resale links.
CFC_RESALE = {
    "Soda": ("sodas", 1),
    "Dasani 500ml": ("dasani 1/2", 1),
    "Dasani 1L": ("dasani 1 ltr", 1),
    "Keringet 500ml": ("keringet 1/2", 1),
    "Keringet 1L": ("keringet 1ltr", 1),
    "Chapati": ("chapati", 1),
    "Mandazi": ("mandazi", 1),
    "Samosas (2 pcs) – Beef": ("samosa", 2),
    "Samosas (2 pcs) – Chicken": ("samosa", 2),
    "Samosas (2 pcs) – Veg": ("samosa", 2),
}


class _Preview(Exception):
    pass


def apply_cfc(c):
    for k, v in CFC_SETTINGS.items():
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
    linked = []
    for item, (stock, qty) in CFC_RESALE.items():
        mi = c.execute("SELECT id FROM menu_items WHERE name = ?", (item,)).fetchone()
        if not mi:
            continue
        ing = c.execute("SELECT id, unit FROM ingredients WHERE name = ?", (stock,)).fetchone()
        if not ing:
            iid = c.execute("INSERT INTO ingredients(name, unit, cost_per_unit, par_level, count_daily) VALUES (?, 'pcs', 0, 0, 1)",
                            (stock,)).lastrowid
        elif ing["unit"] != "pcs":
            print(f"  ! {stock} is measured in {ing['unit']}, not pieces: {item} not linked")
            continue
        else:
            iid = ing["id"]
        if not c.execute("SELECT 1 FROM recipe_lines WHERE menu_item_id = ?", (mi["id"],)).fetchone():
            c.execute("INSERT INTO recipe_lines(menu_item_id, ingredient_id, qty) VALUES (?, ?, ?)", (mi["id"], iid, qty))
            linked.append(f"{item} -> {qty} × {stock}")
    audit(c, None, "settings.profile", "settings", None, {"profile": "cfc", "settings": CFC_SETTINGS, "linked": linked})
    return linked


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else os.path.join(BASE_DIR, "menus", "cfc_menu.csv")
    with open(path, "rb") as f:
        data = f.read()
    init_db()
    try:
        with tx() as c:
            st = importer.import_menu_csv(c, data, None, "--hide-missing" in sys.argv)
            linked = apply_cfc(c) if "--cfc" in sys.argv else []
            if "--preview" in sys.argv:
                raise _Preview()
    except _Preview:
        print("PREVIEW ONLY – nothing saved.")
    except importer.ImportErrors as e:
        sys.exit("Menu NOT loaded:\n" + "\n".join(e.errors))
    print(f"{st['rows']} menu items in {st['categories']} categories: {st['created']} new, {st['updated']} updated, "
          f"{st['unchanged']} unchanged.")
    if st["price_changes"]:
        print("Price changes:", "; ".join(f"{p['item']} {p['old']:g} -> {p['new']:g}" for p in st["price_changes"]))
    if st["hidden_missing"]:
        print(f"Hidden (not in file): {len(st['hidden_missing'])} items, e.g. {', '.join(st['hidden_missing'][:5])}")
    if st["hidden_items"]:
        print("Hidden until a price is confirmed:", ", ".join(st["hidden_items"]))
    if linked:
        print("Linked to stock:", "; ".join(linked))
    if st["flags"]:
        print(f"{len(st['flags'])} items to review (Setup → Menu shows them):")
        for f_ in st["flags"]:
            print("  -", f_)


if __name__ == "__main__":
    main()
