"""Month-to-date records in any common layout, imported with as little typing as possible;
automatic day opening; the analytics behind the dashboards."""
import io
from datetime import date, timedelta

from openpyxl import Workbook

from app import workbook
from conftest import login, ref

D = [(date.today() - timedelta(days=n)) for n in (3, 2, 1)]


def records_csv(sep=",", dayfirst=True):
    fmt = (lambda d: d.strftime("%d/%m/%Y")) if dayfirst else (lambda d: d.isoformat())
    rows = [
        ["Date", "Item", "Unit", "Opening", "Received", "Closing", "Sales", "Waste", "Unit cost", "Remarks"],
        [fmt(D[0]), "Rice", "kg", "20", "", "17.5", "2.5", "", "180", ""],
        [fmt(D[0]), "Chapati", "pcs", "40", "60", "30", "68", "2", "", "overstayed"],
        [fmt(D[0]), "Cooking oil", "", "10", "", "9.2", "0.8", "", "", ""],
        [fmt(D[1]), "Rice", "kg", "", "25", "38", "4.5", "", "", ""],          # opening carried from 17.5
        [fmt(D[1]), "Chapati", "pcs", "", "60", "20", "68", "", "", ""],
        [fmt(D[1]), "Cooking oil", "", "", "", "8.5", "0.7", "", "", ""],
        [fmt(D[2]), "Rice", "kg", "", "", "34", "3.5", "", "", ""],
        [fmt(D[2]), "Chapati", "pcs", "", "60", "15", "64", "1", "", ""],
        [fmt(D[2]), "Cooking oil", "", "", "", "7.9", "0.6", "", "", ""],
    ]
    return "\n".join(sep.join(r) for r in rows).encode()


def test_flat_table_is_recognised_and_carries_opening():
    r = workbook.parse_any(records_csv(), "records.csv")
    assert r["format"] == "table" and r["counts"]["error"] == 0, r["issues"]
    assert [s["dates"][0] for s in r["sheets"]] == [d.isoformat() for d in D]
    rice = {s["dates"][0]: next(x for x in s["rows"] if x["key"] == "rice") for s in r["sheets"]}
    assert rice[D[1].isoformat()]["opening"] == 17.5  # carried over, not 0
    items = {i["key"]: i for i in r["items"]}
    assert items["rice"]["file_unit"] == "kg" and items["rice"]["file_cost"] == 180
    assert items["chapati"]["suggested_unit"] == "pcs"
    assert items["cooking oil"]["suggested_unit"] == "L"  # from the name; no unit column value


def test_semicolon_and_iso_dates_and_excel_sheet():
    assert workbook.parse_any(records_csv(";", dayfirst=False), "x.csv")["counts"]["error"] == 0
    wb = Workbook()
    ws = wb.active
    for row in records_csv().decode().splitlines():
        ws.append(row.split(","))
    buf = io.BytesIO()
    wb.save(buf)
    r = workbook.parse_any(buf.getvalue(), "november records.xlsx")
    assert r["format"] == "table" and len(r["sheets"]) == 3


def test_import_with_automatic_units_then_open_today(cl):
    M = login(cl, "2222")
    f = {"file": ("records.csv", records_csv())}
    pv = cl.post("/api/import/stock-workbook?auto_units=true", headers=M, files=f).json()
    assert pv["needs_unit"] == [] and pv["first_date"] == D[0].isoformat()
    units = {i["key"]: (i["resolved_unit"], i["unit_source"]) for i in pv["items"]}
    assert units["rice"] == ("kg", "file") and units["cooking oil"][0] == "L"
    r = cl.post("/api/import/stock-workbook?commit=true&auto_units=true&open_day=true", headers=M, files=f,
                data={"mapping": "{}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 3 and body["opened_day"]["business_date"] == date.today().isoformat()
    assert "Rice" in body["costs_set"]
    stock = {ln["name"]: ln for ln in cl.get("/api/stock", headers=M).json()["lines"]}
    assert stock["Rice"]["opening"] == 34000 and stock["Chapati"]["opening"] == 15  # last closing carried in
    ing = {i["name"]: i for i in cl.get("/api/ingredients", headers=M).json()}
    assert abs(ing["Rice"]["cost_per_unit"] - 0.18) < 1e-9  # KES 180 per kg stored per gram
    days = cl.get("/api/days", headers=M).json()
    first = next(d for d in days if d["business_date"] == D[0].isoformat())
    chap = next(ln for ln in cl.get(f"/api/reports/day/{first['id']}", headers=M).json()["stock"] if ln["name"] == "Chapati")
    assert chap["wasted"] == 2 and chap["variance"] == 0  # 40 + 60 - 68 - 2 = 30 counted


def test_ambiguous_unit_is_the_only_question(cl):
    M = login(cl, "2222")
    data = b"date,item,closing\n" + f"{D[0].isoformat()},shakes,4.5\n{D[1].isoformat()},shakes,3\n".encode()
    pv = cl.post("/api/import/stock-workbook?auto_units=true", headers=M, files={"file": ("r.csv", data)}).json()
    assert pv["needs_unit"] == ["shakes"]
    r = cl.post("/api/import/stock-workbook?commit=true&auto_units=true", headers=M, files={"file": ("r.csv", data)},
                data={"mapping": '{"shakes": {"unit": "L"}}'})
    assert r.status_code == 200, r.text


def test_bad_dates_block_the_import(cl):
    M = login(cl, "2222")
    data = b"date,item,closing\nsomeday,rice,4\n"
    pv = cl.post("/api/import/stock-workbook?auto_units=true", headers=M, files={"file": ("r.csv", data)}).json()
    assert pv["counts"]["error"] == 1 and pv["issues"][0]["code"] == "bad_date"


def test_auto_open_day_carries_over(cl):
    M = login(cl, "2222")
    cl.post("/api/import/stock-workbook?commit=true&auto_units=true", headers=M,
            files={"file": ("records.csv", records_csv())}, data={"mapping": "{}"})
    rice = next(i for i in cl.get("/api/ingredients", headers=M).json() if i["name"] == "Rice")
    mv = {"ingredient_id": rice["id"], "kind": "waste", "qty": 500, "reason": "Discarded / spoiled", "client_ref": ref()}
    assert cl.post("/api/stock/movement", headers=M, json=mv).status_code == 400  # setting off: day must be opened
    st = cl.get("/api/state", headers=M).json()["settings"]
    body = {k: st[k] for k in ("business_name", "variance_tolerance_pct")} | {"auto_open_day": True}
    A = login(cl, "1234")
    assert cl.put("/api/settings", headers=A, json=body).status_code == 200
    mv["client_ref"] = ref()
    assert cl.post("/api/stock/movement", headers=M, json=mv).status_code == 200
    state = cl.get("/api/state", headers=M).json()
    assert state["day"]["business_date"] == date.today().isoformat()
    line = next(ln for ln in cl.get("/api/stock", headers=M).json()["lines"] if ln["name"] == "Rice")
    assert line["opening"] == 34000 and line["wasted"] == 500


def test_analytics(cl):
    M = login(cl, "2222")
    cl.post("/api/import/stock-workbook?commit=true&auto_units=true&open_day=true", headers=M,
            files={"file": ("records.csv", records_csv())}, data={"mapping": "{}"})
    a = cl.get("/api/analytics?days=7", headers=M).json()
    assert a["money"] is True and a["totals"]["closed_days"] == 3
    assert len(a["daily"]) == 4  # three imported days + today
    rice = next(x for x in a["top_usage"] if x["name"] == "Rice")
    assert rice["qty"] == 2500 + 4500 + 4000 and rice["value"] == round(11000 * 0.18, 2)
    assert a["coverage"]["costed"] >= 1 and isinstance(a["insights"], list)
    C = login(cl, "1111")  # cashier: quantities only
    ac = cl.get("/api/analytics?days=7", headers=C).json()
    assert ac["money"] is False and "value" not in ac["top_usage"][0] and ac["suppliers"] == []
