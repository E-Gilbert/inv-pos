"""Reader for CFC's daily stock-movement workbook (e.g. "oct 2026 stock movement.xlsx").

Layout of every sheet (one sheet per business day, named "1st", "2nd", "4th-5th", ...):

    B: item | C: opening | D: additions | E: closing | F: actual sales | G: system sales | H: variance | I: note

Workbook formulas (reproduced exactly here):

    actual sales (F) = opening + additions - closing          physical usage
    variance     (H) = system sales - actual sales            negative = more left the store than the POS sold

That is the same quantity INV-POS calls the closing variance:

    expected = opening + delivered - sold - waste;   variance = counted - expected
             = (sold + waste) - (opening + delivered - counted)  ==  system - actual   (waste = 0 here)

so imported history lines up with app-generated days without sign changes.

This module only READS the workbook (it never writes to it) and returns plain data plus a
list of discrepancies. `import_history` (in importer.py) commits it to the database.
"""
import hashlib
import io
import re
from collections import OrderedDict
from datetime import date

from openpyxl import load_workbook

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DAY_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)?", re.I)
COLS = {"opening": "opening", "additions": "additions", "closing": "closing",
        "actual sales": "actual", "system sales": "system", "variance": "variance"}
EPS = 1e-6

# Units the workbook can be in, and how they map to the app's base units.
WORKBOOK_UNITS = {"kg": ("g", 1000), "g": ("g", 1), "l": ("ml", 1000), "ml": ("ml", 1), "pcs": ("pcs", 1)}


def norm(name):
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


def period_from_filename(filename):
    """'oct 2026 stock movement.xlsx' -> (2026, 10). None if it can't tell."""
    f = (filename or "").lower()
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s_-]*(20\d\d)\b", f)
    if m:
        return int(m.group(2)), MONTHS[m.group(1)]
    m = re.search(r"\b(20\d\d)[\s_-]*(0?[1-9]|1[0-2])\b", f)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _days_from_sheet(name):
    """'1st' -> [1]; '4th-5th' -> [4, 5]. Returns [] if the name has no day."""
    nums = [int(n) for n in DAY_RE.findall(name)]
    nums = [n for n in nums if 1 <= n <= 31]
    if len(nums) >= 2:
        return list(range(nums[0], nums[-1] + 1)) if nums[-1] >= nums[0] else []
    return nums


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return "bad"


def _parts(formula):
    """'=15+20+25' -> 3 (a count split over several places). Plain '=13.82' -> 1."""
    if isinstance(formula, str) and formula.startswith("="):
        body = formula[1:]
        if re.fullmatch(r"[\d.\s+]+", body):
            return len([p for p in body.split("+") if p.strip()])
    return 0


def _literal_sum(formula):
    body = formula[1:].replace(" ", "")
    if re.fullmatch(r"\d+(\.\d+)?(\+\d+(\.\d+)?)*", body):
        return float(sum(float(p) for p in body.split("+")))
    return None


def suggest_unit(values, name):
    """A SUGGESTION only (the user must confirm). Whole numbers everywhere -> pieces;
    otherwise a unit only when the name makes it obvious."""
    n = norm(name)
    nums = [v for v in values if isinstance(v, float)]
    if nums and all(abs(v - round(v)) < EPS for v in nums):
        return "pcs"
    if any(k in n for k in ("oil", "milk", "juice")):
        return "L"
    if any(k in n for k in ("meat", "goat", "beef", "rice", "flour", "sugar", "steak")):
        return "kg"
    return None


def parse(data: bytes, filename="", year=None, month=None):
    wb_f = load_workbook(io.BytesIO(data), data_only=False)
    wb_v = load_workbook(io.BytesIO(data), data_only=True)
    if not (year and month):
        p = period_from_filename(filename)
        if p:
            year, month = p
    issues = []

    def issue(level, sheet, item, msg, code):
        issues.append({"level": level, "sheet": sheet, "item": item, "message": msg, "code": code})

    sheets = []
    for ws in wb_f.worksheets:
        wv = wb_v[ws.title]
        header_row, cols = None, {}
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 10)):
            found = {COLS[norm(c.value)]: c.column for c in row if isinstance(c.value, str) and norm(c.value) in COLS}
            if "opening" in found and "closing" in found:
                header_row, cols = row[0].row, found
                break
        if not header_row:
            issue("warning", ws.title, None, "No 'opening / closing' header found; sheet skipped.", "no_header")
            continue
        item_col = min(cols.values()) - 1
        note_col = max(cols.values()) + 1
        days = _days_from_sheet(ws.title)
        if not days:
            issue("error", ws.title, None, "Sheet name has no day number; can't date it.", "no_date")
        dates = []
        if days and year and month:
            try:
                dates = [date(year, month, d).isoformat() for d in days]
            except ValueError:
                issue("error", ws.title, None, "Sheet day is not a valid date for the month.", "bad_date")
        if len(days) > 1:
            issue("warning", ws.title, None,
                  f"One sheet covers {len(days)} days ({ws.title}); it is imported as a single period "
                  f"dated {dates[0] if dates else '?'} so daily figures for those days can't be separated.",
                  "multi_day")
        rows, seen = [], set()
        for r in range(header_row + 1, ws.max_row + 1):
            name = ws.cell(r, item_col).value
            if name in (None, ""):
                continue
            key = norm(name)
            if key in seen:
                issue("error", ws.title, str(name), "Item appears twice on the same sheet.", "duplicate_item")
                continue
            seen.add(key)
            vals, raw, parts = {}, {}, {}
            for k, col in cols.items():
                f = ws.cell(r, col).value
                v = wv.cell(r, col).value
                raw[k] = f
                parts[k] = _parts(f)
                if isinstance(f, str) and f.startswith("=") and v is None:
                    # No value saved by Excel (file written by another tool). Simple typed sums
                    # like =15+20+25 are evaluated here; the actual/variance columns are recomputed anyway.
                    lit = _literal_sum(f)
                    if lit is not None:
                        v = lit
                    elif k not in ("actual", "variance"):
                        issue("error", ws.title, str(name), f"{k} is a formula with no saved value; open and save it in Excel.", "no_cached_value")
                elif not isinstance(f, str) or not f.startswith("="):
                    v = f if v is None else v
                vals[k] = _num(v)
            if any(vals[k] == "bad" for k in vals):
                issue("error", ws.title, str(name), "A cell that should be a number contains text.", "text_in_number")
                continue
            note = wv.cell(r, note_col).value
            o, a, c, s = (vals.get("opening") or 0.0), (vals.get("additions") or 0.0), vals.get("closing"), vals.get("system")
            if vals.get("opening") is None:
                issue("warning", ws.title, str(name), "Opening is blank (treated as 0).", "blank_opening")
            if c is None:
                issue("error", ws.title, str(name), "Closing count is blank.", "blank_closing")
                continue
            actual = round(o + a - c, 6)
            variance = round((s or 0) - actual, 6)
            if vals.get("actual") is not None and abs(vals["actual"] - actual) > 1e-4:
                issue("warning", ws.title, str(name),
                      f"'actual sales' cell shows {vals['actual']:g} but opening+additions-closing = {actual:g}; "
                      "the recalculated figure is used.", "formula_mismatch")
            if vals.get("variance") is not None and abs(vals["variance"] - variance) > 1e-4:
                issue("warning", ws.title, str(name),
                      f"'variance' cell shows {vals['variance']:g} but system-actual = {variance:g}; "
                      "the recalculated figure is used.", "formula_mismatch")
            if actual < -EPS:
                issue("warning", ws.title, str(name),
                      f"Closing ({c:g}) is more than opening + additions ({o + a:g}): a delivery was probably not "
                      "written down, or a count is wrong. Imported as recorded.", "negative_usage")
            if s is None:
                issue("warning", ws.title, str(name), "System sales blank.", "blank_system")
            if variance > EPS:
                issue("warning", ws.title, str(name),
                      f"POS sold {s:g} but only {actual:g} left the store (+{variance:g}). Physically impossible unless "
                      "a delivery was not written down, the count is wrong, or another item was rung up instead.",
                      "positive_variance")
            for k in ("opening", "additions", "closing"):
                if parts[k] > 1:
                    issue("info", ws.title, str(name), f"{k} is the sum of {parts[k]} separate counts ({raw[k]}).", "split_count")
            rows.append({
                "item": str(name).strip(), "key": key, "opening": o, "additions": a, "closing": c,
                "actual": actual, "system": s, "variance": variance, "note": str(note).strip() if note else None,
            })
            if note:
                issue("info", ws.title, str(name), f"Note on sheet: {note}", "note")
        sheets.append({"name": ws.title, "days": days, "dates": dates, "rows": rows})

    # Order chronologically, then check gaps and carry-over between consecutive sheets.
    sheets.sort(key=lambda s: (s["days"][0] if s["days"] else 99))
    covered = [d for s in sheets for d in s["days"]]
    for s in sheets:
        if s["days"] and covered.count(s["days"][0]) > 1:
            issue("error", s["name"], None, "Another sheet covers the same day.", "overlap")
    if covered:
        missing = [d for d in range(min(covered), max(covered) + 1) if d not in covered]
        for d in missing:
            issue("warning", None, None, f"No sheet for day {d} of the month: stock movement for that day is unrecorded.", "missing_day")
    for prev, cur in zip(sheets, sheets[1:], strict=False):
        pc = {r["key"]: r for r in prev["rows"]}
        for r in cur["rows"]:
            p = pc.get(r["key"])
            if p is None:
                issue("warning", cur["name"], r["item"], f"Not on the previous sheet ({prev['name']}).", "new_item")
            elif abs(p["closing"] - r["opening"]) > 1e-6:
                issue("warning", cur["name"], r["item"],
                      f"Opening {r['opening']:g} doesn't match previous closing {p['closing']:g} on '{prev['name']}' "
                      f"(difference {r['opening'] - p['closing']:+g}). Imported as recorded.", "carry_over_break")
        for k in pc:
            if k not in {r["key"] for r in cur["rows"]}:
                issue("warning", cur["name"], pc[k]["item"], f"On '{prev['name']}' but missing here.", "dropped_item")

    # Per-item summary across the period.
    items = OrderedDict()
    for s in sheets:
        for r in s["rows"]:
            it = items.setdefault(r["key"], {"item": r["item"], "values": [], "actual": 0.0, "system": 0.0,
                                             "variance": 0.0, "days_off": 0, "additions": 0.0})
            it["values"] += [r["opening"], r["additions"], r["closing"]] + ([r["system"]] if r["system"] is not None else [])
            it["actual"] += r["actual"]
            it["system"] += r["system"] or 0
            it["variance"] += r["variance"]
            it["additions"] += r["additions"]
            it["days_off"] += abs(r["variance"]) > 1e-6
            it["last_closing"] = r["closing"]
    summary = []
    for k, it in items.items():
        summary.append({
            "key": k, "item": it["item"], "suggested_unit": suggest_unit(it["values"], it["item"]),
            "total_actual": round(it["actual"], 4), "total_system": round(it["system"], 4),
            "total_variance": round(it["variance"], 4), "days_with_variance": it["days_off"],
            "total_additions": round(it["additions"], 4), "last_closing": it["last_closing"],
        })

    return {
        "year": year, "month": month, "sha256": hashlib.sha256(data).hexdigest(),
        "sheets": sheets, "items": summary, "issues": issues,
        "counts": {lvl: sum(1 for i in issues if i["level"] == lvl) for lvl in ("error", "warning", "info")},
    }


def is_stock_movement_workbook(data: bytes):
    """True if the first sheet has the opening/closing header (vs. the recipe template)."""
    try:
        ws = load_workbook(io.BytesIO(data), read_only=True).worksheets[0]
        for row in ws.iter_rows(min_row=1, max_row=10, values_only=True):
            names = {norm(v) for v in row if isinstance(v, str)}
            if {"opening", "closing"} <= names:
                return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- flat records (CSV or one-sheet Excel)
#
# Many places keep the month as one long table instead of one sheet per day:
#
#     date | item | opening | received | closing | sales | waste | unit | unit cost | note
#
# Column names vary, so each column is recognised from a list of common names. Only date, item and
# closing are required. A blank opening is carried over from the item's previous closing, so a file
# with closing counts and deliveries alone is enough. The result has the same shape as parse(), so the
# same checks and the same import apply.

TABLE_ALIASES = {
    "date": ("date", "business date", "day", "trading date", "dt"),
    "item": ("item", "item name", "items", "product", "product name", "stock item", "ingredient", "description",
             "name", "particulars"),
    "opening": ("opening", "opening stock", "opening balance", "open", "b/f", "bf", "brought forward", "start"),
    "additions": ("additions", "addition", "added", "received", "receipts", "delivered", "deliveries", "delivery",
                  "purchases", "purchased", "stock in", "in"),
    "closing": ("closing", "closing stock", "closing balance", "closing count", "count", "counted", "physical count",
                "physical", "c/f", "cf", "end"),
    "system": ("system sales", "pos sales", "sales", "sold", "system", "issued", "issues", "micros sales"),
    "waste": ("waste", "wasted", "spoilage", "spoiled", "damaged", "expired"),
    "actual": ("actual sales", "actual usage", "usage", "used", "consumption"),
    "variance": ("variance", "difference", "diff"),
    "note": ("note", "notes", "remark", "remarks", "comment", "comments", "reason"),
    "unit": ("unit", "units", "uom", "unit of measure", "measure"),
    "cost": ("unit cost", "cost", "cost per unit", "buying price", "purchase price", "price per unit", "unit price"),
}
_ALIAS = {norm(a): k for k, names in TABLE_ALIASES.items() for a in names}
UNIT_WORDS = {"kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg",
              "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
              "l": "L", "lt": "L", "ltr": "L", "ltrs": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L",
              "ml": "ml", "millilitre": "ml", "milliliter": "ml",
              "pcs": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs", "each": "pcs", "no": "pcs", "nos": "pcs",
              "units": "pcs", "unit": "pcs", "packet": "pcs", "packets": "pcs", "pkt": "pcs", "bottle": "pcs",
              "bottles": "pcs", "tray": "pcs", "trays": "pcs", "crate": "pcs", "crates": "pcs"}
MONTH_NAMES = {**MONTHS, **{m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}, "sept": 9}


def unit_word(v):
    """'Kgs' -> 'kg', 'litres' -> 'L', 'packets' -> 'pcs'; None if not a unit."""
    return UNIT_WORDS.get(norm(v).rstrip("."), None) if v not in (None, "") else None


def _header_key(v):
    if not isinstance(v, str):
        return None
    n = re.sub(r"[\s_]+", " ", re.sub(r"\(.*?\)", "", v)).strip().lower().rstrip(":")
    return _ALIAS.get(n)


def _table_rows(data, filename):
    """List of row tuples from a CSV or the first sheet of an xlsx that has a date column."""
    name = (filename or "").lower()
    if name.endswith(".csv") or not data[:2] == b"PK":
        import csv
        text = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [tuple(r) for r in csv.reader(io.StringIO(text), dialect)]
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        for r in rows[:15]:
            if "date" in {_header_key(v) for v in r}:
                return rows
    return []


def is_table(data, filename=""):
    """True if the file is a flat table with a date column (CSV, or Excel with one long sheet)."""
    try:
        rows = _table_rows(data, filename)
    except Exception:
        return False
    for r in rows[:15]:
        keys = {_header_key(v) for v in r}
        if {"date", "item", "closing"} <= keys:
            return True
    return False


def _parse_date(v, year, month, dayfirst):
    """date object from a cell. Accepts real dates, 2026-11-03, 03/11/2026, 3 Nov 2026, Nov 3, or just '3rd'."""
    from datetime import datetime
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # Excel serial number
        from datetime import timedelta
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v or "").strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ t].*)?", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})(?: .*)?", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        d, mo = (a, b) if dayfirst else (b, a)
        return date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[\s-]+([a-z]+)\.?,?[\s-]*(\d{4})?", s)
    if m and m.group(2) in MONTH_NAMES:
        return date(int(m.group(3) or year or 0), MONTH_NAMES[m.group(2)], int(m.group(1)))
    m = re.fullmatch(r"([a-z]+)\.?[\s-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s-]*(\d{4})?", s)
    if m and m.group(1) in MONTH_NAMES:
        return date(int(m.group(3) or year or 0), MONTH_NAMES[m.group(1)], int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", s)
    if m and year and month:
        return date(year, month, int(m.group(1)))
    raise ValueError(s)


def parse_table(data: bytes, filename="", year=None, month=None):
    if not (year and month):
        p = period_from_filename(filename)
        if p:
            year, month = p
    issues = []

    def issue(level, sheet, item, msg, code):
        issues.append({"level": level, "sheet": sheet, "item": item, "message": msg, "code": code})

    rows = _table_rows(data, filename)
    header_i, cols = None, {}
    for i, r in enumerate(rows[:15]):
        found = {}
        for j, v in enumerate(r):
            k = _header_key(v)
            if k and k not in found:
                found[k] = j
        if {"date", "item", "closing"} <= set(found):
            header_i, cols = i, found
            break
    empty = {"year": year, "month": month, "sha256": hashlib.sha256(data).hexdigest(), "sheets": [], "items": [],
             "format": "table", "columns": []}
    if header_i is None:
        issue("error", None, None, "No header row with date, item and closing columns was found.", "no_header")
        return empty | {"issues": issues, "counts": {"error": 1, "warning": 0, "info": 0}}

    body = [r for r in rows[header_i + 1:] if any(v not in (None, "") for v in r)]
    cell = lambda r, k: (r[cols[k]] if k in cols and cols[k] < len(r) else None)  # noqa: E731
    # Day-first (03/11/2026) is the default in Kenya; switch only if the file can't be day-first.
    slash = [str(cell(r, "date")) for r in body if isinstance(cell(r, "date"), str)]
    pairs = [tuple(int(x) for x in m.groups()) for m in
             (re.match(r"\s*(\d{1,2})[-/.](\d{1,2})[-/.]\d{2,4}", s) for s in slash) if m]
    dayfirst = not (pairs and any(b > 12 for a, b in pairs) and all(a <= 12 for a, b in pairs))

    by_date, units, costs = OrderedDict(), {}, {}
    for n, r in enumerate(body, start=header_i + 2):
        name = cell(r, "item")
        if name in (None, ""):
            continue
        try:
            d = _parse_date(cell(r, "date"), year, month, dayfirst)
        except (ValueError, TypeError):
            issue("error", f"row {n}", str(name), f"Can't read the date '{cell(r, 'date')}'.", "bad_date")
            continue
        if d is None:
            issue("error", f"row {n}", str(name), "Date is blank.", "no_date")
            continue
        by_date.setdefault(d, []).append((n, r))
        u = unit_word(cell(r, "unit"))
        if u:
            units.setdefault(norm(name), u)
        cv = _num(cell(r, "cost"))
        if isinstance(cv, float) and cv > 0:
            costs[norm(name)] = cv

    sheets, last_closing = [], {}
    for d in sorted(by_date):
        label = d.isoformat()
        out, seen = [], set()
        for n, r in by_date[d]:
            name = str(cell(r, "item")).strip()
            key = norm(name)
            if key in seen:
                issue("error", label, name, f"Item appears twice for {label} (row {n}).", "duplicate_item")
                continue
            seen.add(key)
            vals = {k: _num(cell(r, k)) for k in ("opening", "additions", "closing", "system", "waste", "actual", "variance")}
            if any(v == "bad" for v in vals.values()):
                issue("error", label, name, f"Row {n}: a cell that should be a number contains text.", "text_in_number")
                continue
            c = vals["closing"]
            if c is None:
                issue("error", label, name, f"Row {n}: closing count is blank.", "blank_closing")
                continue
            o = vals["opening"]
            if o is None:
                if key in last_closing:
                    o = last_closing[key]
                else:
                    o = 0.0
                    issue("warning", label, name, "Opening is blank and there is no earlier closing (treated as 0).",
                          "blank_opening")
            a, s, w = vals["additions"] or 0.0, vals["system"], vals["waste"] or 0.0
            actual = round(o + a - c, 6)
            variance = round((s or 0) + w - actual, 6)
            if key in last_closing and abs(last_closing[key] - o) > 1e-6:
                issue("warning", label, name, f"Opening {o:g} doesn't match the previous closing {last_closing[key]:g} "
                      f"(difference {o - last_closing[key]:+g}). Imported as recorded.", "carry_over_break")
            if actual < -EPS:
                issue("warning", label, name, f"Closing ({c:g}) is more than opening + received ({o + a:g}): a delivery "
                      "was probably not written down, or a count is wrong. Imported as recorded.", "negative_usage")
            if s is not None and variance > EPS:
                issue("warning", label, name, f"Sales and waste ({(s or 0) + w:g}) are more than what left the store "
                      f"({actual:g}). A delivery may be missing or a count is wrong.", "positive_variance")
            note = cell(r, "note")
            out.append({"item": name, "key": key, "opening": o, "additions": a, "closing": c, "actual": actual,
                        "system": s, "waste": w, "variance": variance,
                        "note": str(note).strip() if note not in (None, "") else None})
            last_closing[key] = c
        if out:
            sheets.append({"name": label, "days": [d.day], "dates": [label], "rows": out})

    if sheets:
        year, month = int(sheets[0]["dates"][0][:4]), int(sheets[0]["dates"][0][5:7])
        all_dates = [date.fromisoformat(s_["dates"][0]) for s_ in sheets]
        span = (all_dates[-1] - all_dates[0]).days + 1
        missing = sorted(set(all_dates[0].fromordinal(all_dates[0].toordinal() + i) for i in range(span)) - set(all_dates))
        for d in missing[:31]:
            issue("warning", None, None, f"No records for {d.isoformat()}: stock movement for that day is unrecorded.",
                  "missing_day")

    items = OrderedDict()
    for s_ in sheets:
        for r in s_["rows"]:
            it = items.setdefault(r["key"], {"item": r["item"], "values": [], "actual": 0.0, "system": 0.0,
                                             "variance": 0.0, "days_off": 0, "additions": 0.0, "waste": 0.0})
            it["values"] += [r["opening"], r["additions"], r["closing"]] + ([r["system"]] if r["system"] is not None else [])
            it["actual"] += r["actual"]
            it["system"] += r["system"] or 0
            it["waste"] += r["waste"]
            it["variance"] += r["variance"]
            it["additions"] += r["additions"]
            it["days_off"] += abs(r["variance"]) > 1e-6
            it["last_closing"] = r["closing"]
    summary = [{
        "key": k, "item": it["item"], "suggested_unit": suggest_unit(it["values"], it["item"]),
        "file_unit": units.get(k), "file_cost": costs.get(k),
        "total_actual": round(it["actual"], 4), "total_system": round(it["system"], 4), "total_waste": round(it["waste"], 4),
        "total_variance": round(it["variance"], 4), "days_with_variance": it["days_off"],
        "total_additions": round(it["additions"], 4), "last_closing": it["last_closing"],
    } for k, it in items.items()]
    return {
        "year": year, "month": month, "sha256": hashlib.sha256(data).hexdigest(), "format": "table",
        "columns": sorted(cols), "sheets": sheets, "items": summary, "issues": issues,
        "counts": {lvl: sum(1 for i in issues if i["level"] == lvl) for lvl in ("error", "warning", "info")},
    }


def parse_any(data: bytes, filename="", year=None, month=None):
    """One sheet per day (CFC's workbook) or one flat table (CSV / single sheet with a date column)."""
    if (filename or "").lower().endswith(".csv") or data[:2] != b"PK" or is_table(data, filename):
        return parse_table(data, filename, year, month)
    out = parse(data, filename, year, month)
    out["format"] = "sheets"
    return out
