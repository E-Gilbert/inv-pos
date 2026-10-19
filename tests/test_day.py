"""End-to-end test of one business day: open, sell, void, waste, deliver, close, carry over."""
from conftest import ing_id, login, menu_ids, open_shift, ref, sell


def test_full_day(cl):
    M, C = login(cl, "2222"), login(cl, "1111")

    # nothing can be sold before the day is opened
    assert sell(cl, C, [(1, 1)]).status_code == 400

    # cashiers can't open the day
    assert cl.post("/api/day/open", headers=C, json={}).status_code == 403
    counts = {str(i["id"]): (48 if i["unit"] == "pcs" else 20000) for i in cl.get("/api/day/opening-preview", headers=M).json()["rows"]}
    assert cl.post("/api/day/open", headers=M, json={"business_date": "2026-01-01", "opening_counts": counts}).status_code == 200

    # shift required before selling
    menu = menu_ids(cl, C)
    r = sell(cl, C, [(menu["Chicken Mandi"], 1)])
    assert r.status_code == 400 and "shift" in r.json()["detail"].lower()
    open_shift(cl, C, 1000)
    open_shift(cl, M, 0)

    o1 = sell(cl, C, [(menu["Chicken Mandi"], 2), (menu["Soda 500ml"], 1)], "cash", amount_tendered=3000).json()
    assert o1["total"] == 2000 and o1["change"] == 1000

    # M-Pesa needs a valid code
    assert sell(cl, C, [(menu["Goat Mandi"], 1)], "mpesa", payment_ref="x").status_code == 400
    o2 = sell(cl, C, [(menu["Goat Mandi"], 1)], "mpesa", payment_ref="abc1234567").json()

    # only managers void; voiding returns stock
    assert cl.post(f"/api/orders/{o2['id']}/void", headers=C, json={"reason": "x"}).status_code == 403
    assert cl.post(f"/api/orders/{o2['id']}/void", headers=M, json={"reason": "cancelled"}).json()["status"] == "void"

    chicken, rice, soda = ing_id(cl, M, "Chicken (raw)"), ing_id(cl, M, "Basmati rice"), ing_id(cl, M, "Soda 500ml")
    assert cl.post("/api/stock/movement", headers=C, json={"ingredient_id": chicken, "kind": "delivery", "qty": 5000}).status_code == 403
    cl.post("/api/stock/movement", headers=M, json={"ingredient_id": chicken, "kind": "delivery", "qty": 5000})
    cl.post("/api/stock/movement", headers=C, json={"ingredient_id": rice, "kind": "waste", "qty": 500, "reason": "Discarded / spoiled"})

    lines = {l["id"]: l for l in cl.get("/api/stock", headers=M).json()["lines"]}
    assert lines[chicken]["expected"] == 20000 + 5000 - 900          # 2 × 450 g
    assert lines[rice]["expected"] == 20000 - 500 - 500              # 2 × 250 g sold, 500 g wasted
    assert lines[ing_id(cl, M, "Goat meat (raw)")]["expected"] == 20000  # void put it back
    assert lines[soda]["expected"] == 47

    counts = {str(i): {"counted": l["expected"]} for i, l in lines.items() if l["count_daily"]}
    counts[str(chicken)] = {"counted": lines[chicken]["expected"] - 1500}
    counts[str(rice)] = {"counted": lines[rice]["expected"] - 100}  # inside the 2% tolerance

    # open shifts block closing
    r = cl.post("/api/day/close", headers=M, json={"counts": counts})
    assert r.status_code == 400 and "shift" in r.json()["detail"].lower()
    sh = cl.get("/api/state", headers=C).json()["shift"]
    assert sh["expected_cash_now"] == 1000 + 2000
    r = cl.post(f"/api/shifts/{sh['id']}/close", headers=C, json={"counted_cash": 2900})
    assert r.status_code == 400 and "short" in r.json()["detail"]
    r = cl.post(f"/api/shifts/{sh['id']}/close", headers=C, json={"counted_cash": 2900, "notes": "100 missing"})
    assert r.json()["cash_variance"] == -100
    msh = cl.get("/api/state", headers=M).json()["shift"]
    cl.post(f"/api/shifts/{msh['id']}/close", headers=M, json={"counted_cash": 0})

    r = cl.post("/api/day/close", headers=M, json={"counts": counts})
    assert r.status_code == 400 and "Chicken" in r.json()["detail"] and "Basmati" not in r.json()["detail"]

    counts[str(chicken)]["reason"] = "Unexplained"
    assert cl.post("/api/day/close", headers=M, json={"counts": counts}).status_code == 400  # needs a note
    counts[str(chicken)]["note"] = "Kitchen to explain"
    rep = cl.post("/api/day/close", headers=M, json={"counts": counts}).json()
    assert rep["sales"]["total"] == 2000 and len(rep["sales"]["voids"]) == 1
    row = next(l for l in rep["stock"] if l["id"] == chicken)
    assert row["variance"] == -1500 and row["flag"]

    # dates can't go backwards
    assert cl.post("/api/day/open", headers=M, json={"business_date": "2025-12-31"}).status_code == 400
    # tonight's count is tomorrow's opening
    cl.post("/api/day/open", headers=M, json={"business_date": "2026-01-02"})
    lines = {l["id"]: l for l in cl.get("/api/stock", headers=M).json()["lines"]}
    assert lines[chicken]["opening"] == 22600


def test_excel_roundtrip(cl):
    M = login(cl, "2222")
    data = cl.get("/api/excel", headers=M).content
    pv = cl.post("/api/excel?preview=true", headers=M, files={"file": ("x.xlsx", data)})
    assert pv.status_code == 200 and pv.json()["preview"] and pv.json()["menu_items"] == 8
    r = cl.post("/api/excel", headers=M, files={"file": ("x.xlsx", data)})
    assert r.status_code == 200 and r.json()["menu_items"] == 8 and r.json()["new_ingredients"] == 0
    assert ref()
