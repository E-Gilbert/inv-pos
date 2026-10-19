"""Stock status colours, incoming stock, purchase orders, partial receiving, costing, reports."""
from app.logic import classify
from conftest import ing_id, login, menu_ids, open_day, open_shift, ref, sell


def test_classify_rules():
    assert classify(0, 10, 0, 1, 50) == "out"
    assert classify(-3, 10, 0, 1, 50) == "out"
    assert classify(5, 10, 0, 1, 50) == "critical"      # default critical = 50% of reorder
    assert classify(5, 10, 2, 1, 50) == "low"           # own critical level wins
    assert classify(10, 10, 0, 1, 50) == "low"          # AT reorder level = low
    assert classify(11, 10, 0, 1, 50) == "healthy"
    assert classify(1, 0, 0, 1, 50) == "healthy"        # no thresholds set
    assert classify(0, 10, 0, 0, 50) == "inactive"


def _supplier(cl, h):
    return cl.get("/api/suppliers", headers=h).json()[0]["id"]


def test_po_incoming_and_partial_receipt(cl):
    M, S = login(cl, "2222"), login(cl, "3333")
    open_day(cl, M, qty_pcs=10)
    water = ing_id(cl, M, "Water 500ml")  # reorder level 24 -> 10 on hand is critical (<= 12)
    ov = {o["id"]: o for o in cl.get("/api/stock/overview", headers=M).json()}
    assert ov[water]["status"] == "critical" and ov[water]["status_colour"] == "red"
    po = cl.post("/api/purchase-orders", headers=S, json={"supplier_id": _supplier(cl, S), "lines": [
        {"ingredient_id": water, "qty_ordered": 48, "unit_cost": 20}]}).json()
    # a draft is not incoming yet; receiving a draft is refused
    assert cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=S, json={"lines": [{"line_id": po["lines"][0]["id"], "qty": 1}]}).status_code == 400
    po = cl.post(f"/api/purchase-orders/{po['id']}/order", headers=S).json()
    ov = {o["id"]: o for o in cl.get("/api/stock/overview", headers=M).json()}
    assert ov[water]["incoming"] == 48 and ov[water]["on_hand"] == 10  # incoming never counted as available
    line = po["lines"][0]["id"]
    body = {"lines": [{"line_id": line, "qty": 20}], "client_ref": ref()}
    po = cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=S, json=body).json()
    assert po["status"] == "partial" and po["lines"][0]["qty_received"] == 20
    again = cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=S, json=body).json()
    assert again["duplicate"] and again["lines"][0]["qty_received"] == 20
    assert cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=S, json={"lines": [{"line_id": line, "qty": 29}]}).status_code == 400
    ov = {o["id"]: o for o in cl.get("/api/stock/overview", headers=M).json()}
    assert ov[water]["on_hand"] == 30 and ov[water]["incoming"] == 28 and ov[water]["status"] == "healthy"
    po = cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=S, json={"lines": [{"line_id": line, "qty": 28}]}).json()
    assert po["status"] == "received" and po["value_received"] == 960
    ov = {o["id"]: o for o in cl.get("/api/stock/overview", headers=M).json()}
    assert ov[water]["incoming"] == 0 and ov[water]["on_hand"] == 58


def test_receiving_requires_open_day(cl):
    M = login(cl, "2222")
    water = ing_id(cl, M, "Water 500ml")
    po = cl.post("/api/purchase-orders", headers=M, json={"supplier_id": _supplier(cl, M), "lines": [
        {"ingredient_id": water, "qty_ordered": 5}]}).json()
    cl.post(f"/api/purchase-orders/{po['id']}/order", headers=M)
    r = cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=M, json={"lines": [{"line_id": po["lines"][0]["id"], "qty": 5}]})
    assert r.status_code == 400 and "open" in r.json()["detail"]


def test_cancel_partial_po_closes_short(cl):
    M = login(cl, "2222")
    open_day(cl, M)
    water = ing_id(cl, M, "Water 500ml")
    po = cl.post("/api/purchase-orders", headers=M, json={"supplier_id": _supplier(cl, M), "lines": [{"ingredient_id": water, "qty_ordered": 10}]}).json()
    cl.post(f"/api/purchase-orders/{po['id']}/order", headers=M)
    cl.post(f"/api/purchase-orders/{po['id']}/receive", headers=M, json={"lines": [{"line_id": po["lines"][0]["id"], "qty": 4}]})
    po = cl.post(f"/api/purchase-orders/{po['id']}/cancel", headers=M).json()
    assert po["status"] == "received"
    assert next(o for o in cl.get("/api/stock/overview", headers=M).json() if o["id"] == water)["incoming"] == 0


def test_weighted_average_cost(cl):
    M, A = login(cl, "2222"), login(cl, "1234")
    cl.put("/api/settings", headers=A, json={"business_name": "CFC", "variance_tolerance_pct": 2, "cost_method": "weighted_average"})
    open_day(cl, M, qty_pcs=10)
    water = ing_id(cl, M, "Water 500ml")  # seed cost 25
    cl.post("/api/stock/movement", headers=M, json={"ingredient_id": water, "kind": "delivery", "qty": 30, "unit_cost": 35})
    cost = next(i for i in cl.get("/api/ingredients", headers=M).json() if i["id"] == water)["cost_per_unit"]
    assert abs(cost - (10 * 25 + 30 * 35) / 40) < 1e-6


def test_adjustment_and_waste_affect_expected(cl):
    M = login(cl, "2222")
    open_day(cl, M)
    rice = ing_id(cl, M, "Basmati rice")
    assert cl.post("/api/stock/movement", headers=M, json={"ingredient_id": rice, "kind": "adjustment", "qty": 300}).status_code == 400  # reason
    cl.post("/api/stock/movement", headers=M, json={"ingredient_id": rice, "kind": "adjustment", "qty": 300, "reason": "Found stock"})
    cl.post("/api/stock/movement", headers=M, json={"ingredient_id": rice, "kind": "waste", "qty": 100, "reason": "Overstayed / expired"})
    line = next(l for l in cl.get("/api/stock", headers=M).json()["lines"] if l["id"] == rice)
    assert line["adjusted"] == 300 and line["wasted"] == 100 and line["expected"] == 20200


def test_cogs_and_gross_profit(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    open_day(cl, M)
    open_shift(cl, C)
    menu = menu_ids(cl, C)
    sell(cl, C, [(menu["Chips (regular)"], 2)])  # 2 × (300 g potato × 0.08 + 40 ml oil × 0.30) = 2 × 36 = 72
    d = cl.get("/api/state", headers=M).json()["day"]["business_date"]
    rep = cl.get("/api/reports/period", headers=M, params={"date_from": d, "date_to": d}).json()
    assert rep["collected"] == 500 and rep["cogs"] == 72 and rep["gross_profit"] == 428 and rep["gross_margin_pct"] == 85.6
    # cost change later does not rewrite history (cost was snapshotted at sale)
    pot = ing_id(cl, M, "Potatoes")
    i = next(x for x in cl.get("/api/ingredients", headers=M).json() if x["id"] == pot)
    cl.put(f"/api/ingredients/{pot}", headers=M, json={**{k: i[k] for k in ("name", "unit", "par_level", "count_daily", "active")}, "cost_per_unit": 1})
    assert cl.get("/api/reports/period", headers=M, params={"date_from": d, "date_to": d}).json()["cogs"] == 72
    A = login(cl, "1234")
    assert any(a["action"] == "ingredient.cost_change" for a in cl.get("/api/audit", headers=A).json())


def test_csv_export_neutralises_formulas(cl):
    M = login(cl, "2222")
    cl.post("/api/ingredients", headers=M, json={"name": "=HYPERLINK(\"http://x\")", "unit": "pcs"})
    body = cl.get("/api/export/stock.csv", headers=M).content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body and "\n=HYPERLINK" not in body


def test_reorder_suggestion_needs_history(cl):
    M = login(cl, "2222")
    open_day(cl, M, qty_pcs=5)
    water = ing_id(cl, M, "Water 500ml")
    ov = next(o for o in cl.get("/api/stock/overview", headers=M).json() if o["id"] == water)
    assert ov["status"] == "critical" and ov["suggested_order"] is None  # not enough history to suggest
