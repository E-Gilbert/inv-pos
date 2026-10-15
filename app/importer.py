"""Excel import/export for ingredients, menu and recipes.

Template (two sheets):
  Ingredients: name | unit (g/ml/pcs) | cost_per_unit (KES) | par_level | count_daily (yes/no)
  Recipes:     menu_item | category | price | ingredient | qty_per_portion

One row in Recipes = one ingredient in one dish. A dish with 4 ingredients has 4 rows;
category and price only need to be filled on the dish's first row.

CFC's own sheet will probably look different; once we have it, either fill this
template from it or add a small adapter function here.
"""
import io

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

ING_HEADERS = ["name", "unit", "cost_per_unit", "par_level", "count_daily"]
REC_HEADERS = ["menu_item", "category", "price", "ingredient", "qty_per_portion"]
UNIT_ALIASES = {
    "g": "g", "gram": "g", "grams": "g", "gm": "g", "gms": "g",
    "ml": "ml", "millilitre": "ml", "milliliter": "ml",
    "pcs": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs", "each": "pcs", "unit": "pcs",
}
# Convert bigger units to the base unit so staff can type what they're used to.
SCALE_UNITS = {"kg": ("g", 1000), "kgs": ("g", 1000), "l": ("ml", 1000), "litre": ("ml", 1000),
               "liter": ("ml", 1000), "ltr": ("ml", 1000)}


def _rows(ws):
    it = ws.iter_rows(values_only=True)
    try:
        header = [str(h).strip().lower() if h is not None else "" for h in next(it)]
    except StopIteration:
        return []
    out = []
    for n, row in enumerate(it, start=2):
        if row is None or all(v in (None, "") for v in row):
            continue
        out.append((n, {header[i]: row[i] for i in range(min(len(header), len(row))) if header[i]}))
    return out


def _yes(v):
    return str(v).strip().lower() in ("1", "yes", "y", "true", "daily") if v not in (None, "") else True


def import_workbook(c, data: bytes):
    wb = load_workbook(io.BytesIO(data), data_only=True)
    sheets = {s.lower(): wb[s] for s in wb.sheetnames}
    if "ingredients" not in sheets or "recipes" not in sheets:
        raise ValueError("The workbook needs two sheets named 'Ingredients' and 'Recipes'. Download the template.")

    errors, stats = [], {"ingredients": 0, "menu_items": 0, "recipe_lines": 0}

    for n, r in _rows(sheets["ingredients"]):
        name = str(r.get("name") or "").strip()
        unit_raw = str(r.get("unit") or "").strip().lower()
        if not name:
            errors.append(f"Ingredients row {n}: name is empty.")
            continue
        scale = 1
        if unit_raw in SCALE_UNITS:
            unit, scale = SCALE_UNITS[unit_raw]
        else:
            unit = UNIT_ALIASES.get(unit_raw)
        if not unit:
            errors.append(f"Ingredients row {n} ({name}): unit '{unit_raw}' must be g, ml or pcs (kg/L are converted).")
            continue
        try:
            # cost given per kg/L gets converted to cost per g/ml
            cost = float(r.get("cost_per_unit") or 0) / scale
            par = float(r.get("par_level") or 0) * scale
        except (TypeError, ValueError):
            errors.append(f"Ingredients row {n} ({name}): cost and par level must be numbers.")
            continue
        c.execute(
            """INSERT INTO ingredients(name, unit, cost_per_unit, par_level, count_daily) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET unit = excluded.unit, cost_per_unit = excluded.cost_per_unit,
                   par_level = excluded.par_level, count_daily = excluded.count_daily, active = 1""",
            (name, unit, cost, par, 1 if _yes(r.get("count_daily")) else 0),
        )
        stats["ingredients"] += 1

    current_item, seen_items = None, set()
    for n, r in _rows(sheets["recipes"]):
        item_name = str(r.get("menu_item") or "").strip() or current_item
        if not item_name:
            errors.append(f"Recipes row {n}: menu_item is empty.")
            continue
        current_item = item_name
        mi = c.execute("SELECT id FROM menu_items WHERE name = ?", (item_name,)).fetchone()

        cat, price = r.get("category"), r.get("price")
        if cat not in (None, "") or price not in (None, ""):
            cat_id = None
            if cat not in (None, ""):
                c.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (str(cat).strip(),))
                cat_id = c.execute("SELECT id FROM categories WHERE name = ?", (str(cat).strip(),)).fetchone()["id"]
            try:
                price_f = float(price) if price not in (None, "") else None
            except (TypeError, ValueError):
                errors.append(f"Recipes row {n} ({item_name}): price must be a number.")
                continue
            if mi:
                if cat_id:
                    c.execute("UPDATE menu_items SET category_id = ? WHERE id = ?", (cat_id, mi["id"]))
                if price_f is not None:
                    c.execute("UPDATE menu_items SET price = ?, active = 1 WHERE id = ?", (price_f, mi["id"]))
            else:
                if price_f is None:
                    errors.append(f"Recipes row {n} ({item_name}): new menu item needs a price.")
                    continue
                c.execute("INSERT INTO menu_items(name, category_id, price) VALUES (?, ?, ?)", (item_name, cat_id, price_f))
            mi = c.execute("SELECT id FROM menu_items WHERE name = ?", (item_name,)).fetchone()
        if not mi:
            errors.append(f"Recipes row {n}: '{item_name}' isn't on the menu yet; give it a price.")
            continue

        if mi["id"] not in seen_items:
            # The sheet is the source of truth: replace the dish's whole recipe.
            c.execute("DELETE FROM recipe_lines WHERE menu_item_id = ?", (mi["id"],))
            seen_items.add(mi["id"])
            stats["menu_items"] += 1

        ing_name = str(r.get("ingredient") or "").strip()
        if not ing_name:
            continue  # a dish with no tracked ingredients (allowed)
        ing = c.execute("SELECT id, unit FROM ingredients WHERE name = ?", (ing_name,)).fetchone()
        if not ing:
            errors.append(f"Recipes row {n} ({item_name}): ingredient '{ing_name}' isn't in the Ingredients sheet.")
            continue
        try:
            qty = float(r.get("qty_per_portion"))
            assert qty > 0
        except (TypeError, ValueError, AssertionError):
            errors.append(f"Recipes row {n} ({item_name} / {ing_name}): qty_per_portion must be more than 0.")
            continue
        c.execute(
            """INSERT INTO recipe_lines(menu_item_id, ingredient_id, qty) VALUES (?, ?, ?)
               ON CONFLICT(menu_item_id, ingredient_id) DO UPDATE SET qty = recipe_lines.qty + excluded.qty""",
            (mi["id"], ing["id"], qty),
        )
        stats["recipe_lines"] += 1

    if errors:
        # Nothing is saved unless the whole sheet is clean.
        raise ValueError("\n".join(errors[:40]) + (f"\n…and {len(errors) - 40} more" if len(errors) > 40 else ""))
    return stats


def _style_header(ws, headers, widths):
    ws.append(headers)
    for i, cell in enumerate(ws[1]):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="7A2E1D")
        ws.column_dimensions[cell.column_letter].width = widths[i]
    ws.freeze_panes = "A2"


def export_workbook(c) -> bytes:
    """Current data in template format. Empty database -> blank template with examples."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Ingredients"
    _style_header(ws, ING_HEADERS, [28, 8, 16, 12, 13])
    dv = DataValidation(type="list", formula1='"g,ml,pcs,kg,L"', allow_blank=False)
    ws.add_data_validation(dv)
    dv.add("B2:B1000")
    ings = c.execute("SELECT * FROM ingredients WHERE active = 1 ORDER BY name").fetchall()
    for i in ings:
        ws.append([i["name"], i["unit"], i["cost_per_unit"], i["par_level"], "yes" if i["count_daily"] else "no"])
    if not ings:
        ws.append(["Basmati rice", "kg", 250, 10, "yes"])
        ws.append(["Chicken (raw)", "kg", 550, 8, "yes"])

    wr = wb.create_sheet("Recipes")
    _style_header(wr, REC_HEADERS, [26, 16, 10, 26, 16])
    rows = c.execute(
        """SELECT m.name, c.name AS cat, m.price, i.name AS ing, rl.qty, m.id
           FROM menu_items m LEFT JOIN categories c ON c.id = m.category_id
           LEFT JOIN recipe_lines rl ON rl.menu_item_id = m.id
           LEFT JOIN ingredients i ON i.id = rl.ingredient_id
           WHERE m.active = 1 ORDER BY c.sort, c.name, m.name, i.name"""
    ).fetchall()
    last = None
    for r in rows:
        first = r["id"] != last
        wr.append([r["name"], r["cat"] if first else None, r["price"] if first else None, r["ing"], r["qty"]])
        last = r["id"]
    if not rows:
        wr.append(["Chicken Mandi", "Mandi", 950, "Basmati rice", 250])
        wr.append(["Chicken Mandi", None, None, "Chicken (raw)", 400])

    notes = wb.create_sheet("How to fill")
    for line in [
        "INGREDIENTS sheet: one row per raw material.",
        "  unit: g, ml or pcs. You may type kg or L and the system converts (cost per kg -> per g).",
        "  count_daily: yes = must be counted every closing (meat, rice, oil). no = weekly items.",
        "  par_level: when stock falls below this, it shows as 'reorder'. Same unit as you typed.",
        "",
        "RECIPES sheet: one row per ingredient per dish.",
        "  qty_per_portion is in the ingredient's BASE unit (g, ml, pcs), for ONE plate.",
        "  Category and price only need to be on the first row of each dish.",
        "  Use the RAW weight (e.g. raw chicken before cooking), so cooking loss is included.",
        "",
        "Importing replaces each listed dish's recipe. If any row has an error, nothing is saved.",
    ]:
        notes.append([line])
    notes.column_dimensions["A"].width = 100

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
