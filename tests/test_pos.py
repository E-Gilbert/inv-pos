"""Checkout: idempotency, concurrency, discounts, tax, M-Pesa duplicates, refunds, shifts."""
import threading

from app import logic
from app.db import tx
from conftest import ing_id, login, menu_ids, open_day, open_shift, ref, sell


def _setup(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    open_day(cl, M)
    open_shift(cl, C, 500)
    open_shift(cl, M, 0)
    return M, C, menu_ids(cl, C)


def test_retry_with_same_key_never_duplicates(cl):
    M, C, menu = _setup(cl)
    body = {"items": [{"menu_item_id": menu["Chicken Mandi"], "qty": 1}], "payment_method": "card", "client_ref": ref()}
    a = cl.post("/api/orders", headers=C, json=body).json()
    b = cl.post("/api/orders", headers=C, json=body).json()
    assert a["id"] == b["id"] and b["duplicate"] and not a["duplicate"]
    assert len(cl.get("/api/orders", headers=C).json()) == 1
    chicken = ing_id(cl, M, "Chicken (raw)")
    line = next(l for l in cl.get("/api/stock", headers=M).json()["lines"] if l["id"] == chicken)
    assert line["sold"] == 450  # deducted once
    body["items"][0]["qty"] = 2  # same key, different order -> refused
    assert cl.post("/api/orders", headers=C, json=body).status_code == 409


def test_concurrent_tills(cl):
    M, C, menu = _setup(cl)
    staff = {"id": 3, "role": "cashier"}
    with tx() as c:
        staff["id"] = c.execute("SELECT id FROM staff WHERE name='Cashier Demo'").fetchone()["id"]
    errors, ids = [], []

    def till():
        try:
            with tx() as c:
                oid, _ = logic.create_order(c, staff, [{"menu_item_id": menu["Soda 500ml"], "qty": 1}], "card", client_ref=ref())
                ids.append(oid)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=till) for _ in range(25)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    nums = [o["number"] for o in cl.get("/api/orders", headers=C).json()]
    assert sorted(nums) == list(range(1, 26))
    soda = ing_id(cl, M, "Soda 500ml")
    assert next(l for l in cl.get("/api/stock", headers=M).json()["lines"] if l["id"] == soda)["expected"] == 48 - 25


def test_discount_needs_manager_approval(cl):
    M, C, menu = _setup(cl)
    item = [(menu["Chicken Mandi"], 1)]
    r = sell(cl, C, item, discount={"type": "pct", "value": 10, "reason": "regular"})
    assert r.status_code == 400 and "manager" in r.json()["detail"]
    assert sell(cl, C, item, discount={"type": "pct", "value": 10}, approver_pin="2222").status_code == 400  # no reason
    assert sell(cl, C, item, discount={"type": "pct", "value": 10, "reason": "x"}, approver_pin="1111").status_code == 400
    o = sell(cl, C, item, discount={"type": "pct", "value": 10, "reason": "regular"}, approver_pin="2222").json()
    assert o["subtotal"] == 950 and o["discount"] == 95 and o["total"] == 855
    assert sell(cl, C, item, discount={"type": "amount", "value": 2000, "reason": "x"}, approver_pin="2222").status_code == 400


def test_tax_inclusive_and_exclusive(cl):
    M, C, menu = _setup(cl)
    A = login(cl, "1234")
    base = {"business_name": "CFC", "variance_tolerance_pct": 2, "tax_rate_pct": 16, "tax_inclusive": True}
    assert cl.put("/api/settings", headers=A, json=base).status_code == 200
    o = sell(cl, C, [(menu["Chicken Mandi"], 1)]).json()
    assert o["total"] == 950 and o["tax"] == 131.03  # 950 × 16/116
    cl.put("/api/settings", headers=A, json=base | {"tax_inclusive": False})
    o = sell(cl, C, [(menu["Chicken Mandi"], 1)]).json()
    assert o["total"] == 1102 and o["tax"] == 152
    # cash must cover the taxed total
    assert sell(cl, C, [(menu["Chicken Mandi"], 1)], "cash", amount_tendered=1000).status_code == 400


def test_mpesa_code_cannot_be_reused(cl):
    M, C, menu = _setup(cl)
    assert sell(cl, C, [(menu["Kachumbari"], 1)], "mpesa", payment_ref="QWE1234567").status_code == 200
    r = sell(cl, C, [(menu["Kachumbari"], 1)], "mpesa", payment_ref="qwe1234567")
    assert r.status_code == 400 and "already used" in r.json()["detail"]


def test_prices_come_from_server(cl):
    M, C, menu = _setup(cl)
    r = cl.post("/api/orders", headers=C, json={"items": [{"menu_item_id": menu["Goat Mandi"], "qty": 1, "price": 1}],
                                               "payment_method": "card", "client_ref": ref()})
    assert r.json()["total"] == 1200
    assert sell(cl, C, [(menu["Goat Mandi"], 0)]).status_code == 422
    assert sell(cl, C, [(99999, 1)]).status_code == 400


def test_partial_and_full_refund(cl):
    M, C, menu = _setup(cl)
    A = login(cl, "1234")
    cl.put("/api/settings", headers=A, json={"business_name": "CFC", "variance_tolerance_pct": 2, "cashier_max_discount_pct": 50})
    o = sell(cl, C, [(menu["Soda 500ml"], 3)], "cash", amount_tendered=500,
             discount={"type": "amount", "value": 10, "reason": "promo"}).json()
    assert o["total"] == 290
    line = o["items"][0]
    # cashier needs a manager PIN
    r = cl.post(f"/api/orders/{o['id']}/refund", headers=C, json={"lines": [{"order_item_id": line["id"], "qty": 1}], "reason": "Other"})
    assert r.status_code == 400
    r1 = cl.post(f"/api/orders/{o['id']}/refund", headers=C, json={
        "lines": [{"order_item_id": line["id"], "qty": 1}], "reason": "Wrong item served", "restock": True,
        "approver_pin": "2222", "client_ref": ref()}).json()
    assert r1["refunded"] == 96.67
    assert cl.post(f"/api/orders/{o['id']}/refund", headers=M, json={
        "lines": [{"order_item_id": line["id"], "qty": 3}], "reason": "x"}).status_code == 400  # only 2 left
    r2 = cl.post(f"/api/orders/{o['id']}/refund", headers=M, json={
        "lines": [{"order_item_id": line["id"], "qty": 2}], "reason": "Customer complaint"}).json()
    assert r2["refunded"] == 290  # exact, no lost cents
    soda = ing_id(cl, M, "Soda 500ml")
    assert next(l for l in cl.get("/api/stock", headers=M).json()["lines"] if l["id"] == soda)["expected"] == 48 - 3 + 1
    assert cl.post(f"/api/orders/{o['id']}/void", headers=M, json={"reason": "x"}).status_code == 400
    sh = cl.get("/api/state", headers=C).json()["shift"]
    assert sh["expected_cash_now"] == 500 + 290 - 96.67  # cashier's till paid out the first (cash) refund
    rep = cl.get(f"/api/reports/day/{o['day_id']}", headers=M).json()
    assert rep["summary"]["refunds"] == 290 and rep["summary"]["net_sales_ex_tax"] == 0


def test_refund_idempotent(cl):
    M, C, menu = _setup(cl)
    o = sell(cl, C, [(menu["Soda 500ml"], 2)]).json()
    body = {"lines": [{"order_item_id": o["items"][0]["id"], "qty": 1}], "reason": "Other", "client_ref": ref()}
    a = cl.post(f"/api/orders/{o['id']}/refund", headers=M, json=body).json()
    b = cl.post(f"/api/orders/{o['id']}/refund", headers=M, json=body).json()
    assert a["refund_id"] == b["refund_id"] and b["duplicate"] and b["refunded"] == 100
