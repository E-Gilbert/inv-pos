"""MICROS-style management reports: figures checked by hand for a known day."""
from conftest import login, menu_ids, open_day, open_shift, ref, sell

DAY = "2026-01-01"


def _scenario(cl):
    M, C, A = login(cl, "2222"), login(cl, "1111"), login(cl, "1234")
    cl.put("/api/settings", headers=A, json={"business_name": "CFC", "variance_tolerance_pct": 2, "tax_rate_pct": 16,
                                             "tax_inclusive": True, "ask_guests": True})
    cats = {c["name"]: c["id"] for c in cl.get("/api/menu", headers=M).json()["categories"]}
    for name in ("Mandi", "Rice", "Sides"):
        assert cl.put(f"/api/categories/{cats[name]}", headers=M, json={"major_group": "Food"}).status_code == 200
    cl.put(f"/api/categories/{cats['Drinks']}", headers=M, json={"major_group": "Beverage"})
    open_day(cl, M, date=DAY)
    open_shift(cl, C, 1000)
    open_shift(cl, M, 0)
    menu = menu_ids(cl, C)
    o1 = sell(cl, C, [(menu["Chicken Mandi"], 2)], "cash", amount_tendered=2000, order_type="Dine in", guests=2).json()
    o2 = sell(cl, C, [(menu["Soda 500ml"], 3)], "mpesa", payment_ref="ABC1234567", order_type="Takeaway",
              discount={"type": "pct", "value": 10, "reason": "Regular"}, approver_pin="2222").json()
    o3 = sell(cl, M, [(menu["Goat Mandi"], 1)], "card", order_type="Delivery", guests=1).json()
    o4 = sell(cl, C, [(menu["Kachumbari"], 1)], "card").json()
    assert cl.post(f"/api/orders/{o4['id']}/void", headers=M, json={"reason": "Rung by mistake"}).status_code == 200
    r = cl.post(f"/api/orders/{o2['id']}/refund", headers=M, json={
        "lines": [{"order_item_id": o2["items"][0]["id"], "qty": 1}], "reason": "Wrong item served", "restock": True, "client_ref": ref()})
    assert r.status_code == 200, r.text
    assert (o1["tax"], o2["tax"], o3["tax"]) == (262.07, 37.24, 165.52)
    return M, C, A


def run(cl, h, key):
    r = cl.get(f"/api/reports/run/{key}", headers=h, params={"date_from": DAY, "date_to": DAY})
    assert r.status_code == 200, r.text
    return r.json()


def sec(rep, title):
    return next(s for s in rep["sections"] if s["title"] == title)


def test_sales_detail(cl):
    M, C, A = _scenario(cl)
    rep = run(cl, M, "sales")
    s = {r["item"]: r["value"] for r in sec(rep, "Sales summary")["rows"]}
    assert s["Gross sales (menu prices)"] == 1900 + 300 + 1200
    assert s["− Discounts"] == 30
    assert s["− Tax in sales"] == 464.83
    assert s["− Returns / refunds (ex tax)"] == 77.59        # 90.00 refunded incl. 12.41 tax
    assert s["Net sales (ex tax)"] == 2827.58
    assert s["Collected at checkout (incl. tax)"] == 3370
    assert s["Checks (paid orders)"] == 3 and s["Average check (collected)"] == 1123.33
    assert s["Guests (where recorded)"] == 3 and s["Average per guest (net)"] == 890.80
    assert s["Voided checks"] == 1 and s["Voided amount"] == 100
    t = {r["method"]: r for r in sec(rep, "Tender media (payments)")["rows"]}
    assert t["M-Pesa"]["collected"] == 270 and t["M-Pesa"]["refunds"] == 90 and t["M-Pesa"]["net"] == 180
    assert t["Cash"]["collected"] == 1900 and t["Card"]["checks"] == 1
    ot = {r["type"]: r for r in sec(rep, "Order types")["rows"]}
    assert ot["Dine in"]["checks"] == 1 and ot["Takeaway"]["net"] == 232.76 and ot["Delivery"]["guests"] == 1
    day = sec(rep, "By business day")["totals"]
    assert day["net"] == 2827.58 and day["tax"] == 452.42  # 464.83 − 12.41 refunded tax
    # same definitions as the period report
    period = cl.get("/api/reports/period", headers=M, params={"date_from": DAY, "date_to": DAY}).json()
    assert period["net_sales_ex_tax"] == s["Net sales (ex tax)"]


def test_groups_and_menu_items_add_up(cl):
    M, C, A = _scenario(cl)
    mg = {r["group"]: r for r in sec(run(cl, M, "major_group"), "Major group sales")["rows"]}
    assert mg["Food"]["net"] == 2672.41 and mg["Beverage"]["net"] == 155.17 and mg["Beverage"]["ret_qty"] == 1
    assert round(mg["Food"]["net"] + mg["Beverage"]["net"], 2) == 2827.58
    items = {r["item"]: r for r in sec(run(cl, M, "menu_item"), "Menu item sales")["rows"]}
    soda = items["Soda 500ml"]
    assert (soda["qty"], soda["ret_qty"], soda["gross"], soda["discount"], soda["returns"], soda["net"]) == (3, 1, 300, 30, 77.59, 155.17)
    assert soda["cost"] == 90  # 3 × 45 sold − 1 × 45 restocked
    assert items["Chicken Mandi"]["cost"] == round(2 * (250 * 0.25 + 450 * 0.55 + 30 * 0.30 + 40 * 0.12 + 8 * 1.20), 2)
    assert "Kachumbari" not in items  # voided check is not a sale


def test_employee_reason_codes_and_cashier(cl):
    M, C, A = _scenario(cl)
    emp = {r["employee"]: r for r in sec(run(cl, M, "employee"), "Employee sales")["rows"]}
    assert emp["Cashier Demo"]["checks"] == 2 and emp["Cashier Demo"]["collected"] == 2170 and emp["Cashier Demo"]["voids"] == 1
    assert emp["Manager Demo"]["refunds"] == 1 and emp["Manager Demo"]["refund_amt"] == 90 and emp["Manager Demo"]["disc_approved"] == 30
    d = run(cl, M, "discounts")
    assert sec(d, "Discount detail")["rows"][0]["approver"] == "Manager Demo" and sec(d, "Discount detail")["rows"][0]["pct"] == 10
    v = sec(run(cl, M, "voids"), "Voids by reason")["rows"]
    assert v == [{"reason": "Rung by mistake", "count": 1, "total": 100}]
    rf = sec(run(cl, M, "refunds"), "Refund detail")["rows"][0]
    assert rf["reason"] == "Wrong item served" and rf["restocked"] == "yes" and rf["method"] == "M-Pesa"
    sh = {r["cashier"]: r for r in sec(run(cl, M, "cashier"), "Cash shifts")["rows"]}
    assert sh["Cashier Demo"]["expected"] == 1000 + 1900 and sh["Cashier Demo"]["closed"] == "open"
    gc = sec(run(cl, M, "guest_checks"), "Guest check journal")
    assert [r["status"] for r in gc["rows"]] == ["paid", "part refunded", "paid", "VOID"] and gc["totals"]["total"] == 3370


def test_time_and_serving_periods(cl):
    M, C, A = _scenario(cl)
    tp = sec(run(cl, M, "time_period"), "Sales by hour")
    assert tp["totals"]["checks"] == 3 and tp["totals"]["net"] == 2905.17  # before refunds
    sp = sec(run(cl, M, "serving_period"), "Sales by serving period")
    assert [r["slot"] for r in sp["rows"]][:3] == ["Breakfast", "Lunch", "Dinner"] and sp["totals"]["checks"] == 3
    bad = {"business_name": "CFC", "variance_tolerance_pct": 2, "serving_periods": "Lunch=11:00-16:00;Late=15:00-18:00"}
    r = cl.put("/api/settings", headers=A, json=bad)
    assert r.status_code == 422 and "overlap" in r.json()["detail"]


def test_tax_report_and_csv_and_permissions(cl):
    M, C, A = _scenario(cl)
    net = {r["item"]: r["value"] for r in sec(run(cl, M, "tax"), "Net VAT")["rows"]}
    assert net == {"VAT on sales": 464.83, "− VAT on refunds": 12.41, "Net VAT": 452.42}
    csv = cl.get("/api/reports/run/menu_item", headers=M, params={"date_from": DAY, "date_to": DAY, "format": "csv"})
    assert csv.status_code == 200 and "Menu item sales" in csv.content.decode("utf-8-sig")
    assert cl.get("/api/reports/run/sales", headers=C, params={"date_from": DAY, "date_to": DAY}).status_code == 403
    assert cl.get("/api/reports/run/nope", headers=M, params={"date_from": DAY, "date_to": DAY}).status_code == 404
    assert cl.get("/api/reports/run/sales", headers=M, params={"date_from": "2026-02-01", "date_to": "2026-01-01"}).status_code == 400
    empty = cl.get("/api/reports/run/sales", headers=M,
                                                     params={"date_from": "2025-01-01", "date_to": "2025-01-02"}).json()
    assert empty["days"] == 0 and "No business days" in empty["notes"][0]


def test_order_type_validation(cl):
    M, C, A = _scenario(cl)
    menu = menu_ids(cl, C)
    r = sell(cl, C, [(menu["Kachumbari"], 1)], order_type="Drive-through")
    assert r.status_code == 400 and "order type" in r.json()["detail"]
    assert sell(cl, C, [(menu["Kachumbari"], 1)], guests=501).status_code == 422
