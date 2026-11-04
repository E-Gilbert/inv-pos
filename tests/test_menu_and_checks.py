"""CFC menu catalogue (VAT-inclusive prices, options, sale days) and open checks (guest checks)."""
import os
import sys

from app.db import tx
from conftest import login, open_day, open_shift, ref, sell

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MENU = open(os.path.join(ROOT, "menus", "cfc_menu.csv"), "rb").read()
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import load_menu  # noqa: E402


def load_cfc(cl, M):
    r = cl.post("/api/import/menu?hide_missing=true", headers=M, files={"file": ("cfc_menu.csv", MENU)})
    assert r.status_code == 200, r.text
    with tx() as c:
        load_menu.apply_cfc(c)
    return r.json()


def items(cl, h):
    return {i["name"]: i for i in cl.get("/api/menu", headers=h).json()["items"]}


def test_menu_import_is_complete_and_idempotent(cl):
    M = login(cl, "2222")
    pv = cl.post("/api/import/menu?preview=true", headers=M, files={"file": ("cfc_menu.csv", MENU)}).json()
    assert pv["preview"] and pv["created"] == 328
    assert items(cl, M)["Chicken Mandi"]  # preview saved nothing
    st = load_cfc(cl, M)
    assert st["created"] == 328 and st["categories"] == 25 and len(st["hidden_missing"]) == 8
    menu = items(cl, M)
    assert len(menu) == 325 and "Chicken Mandi" not in menu       # demo items hidden; 3 unpriced items hidden
    assert {"Black iced", "Grape smoothie", "Make your own combo"}.isdisjoint(menu)
    assert menu["Butter chicken"]["price"] == 800 and menu["Sharing feast"]["price"] == 6000
    assert menu["Shawarma – Beef"]["variant_group"] == "Shawarma" and menu["Shawarma – Beef"]["variant"] == "Beef"
    assert [menu[f"BBQ chicken pizza – {s}"]["price"] for s in ("Regular", "Medium", "Large", "Mega")] == [600, 800, 1000, 1200]
    assert menu["Thursday special – 2 large pizzas"]["available_days"] == "Thu"
    again = cl.post("/api/import/menu", headers=M, files={"file": ("cfc_menu.csv", MENU)}).json()
    assert again["unchanged"] == 328 and again["created"] == again["updated"] == 0
    # export -> import round trip changes nothing
    out = cl.get("/api/export/menu.csv", headers=M).content
    rt = cl.post("/api/import/menu?preview=true", headers=M, files={"file": ("menu.csv", out)}).json()
    assert rt["created"] == 0 and rt["updated"] == 0


def test_menu_import_validation(cl):
    M = login(cl, "2222")
    bad = ("category,name,variant,price,days,active\n"
           "Drinks,Juice,,0,,1\nDrinks,Tea,,abc,,1\nDrinks,Cake,,100,Funday,1\nDrinks,Soda,,100,,1\nDrinks,Soda,,120,,1\n").encode()
    r = cl.post("/api/import/menu", headers=M, files={"file": ("m.csv", bad)})
    assert r.status_code == 400
    errs = " ".join(r.json()["errors"])
    assert "price is 0" in errs and "must be a number" in errs and "not a weekday" in errs and "appears twice" in errs
    assert "Soda" not in items(cl, M)  # all-or-nothing


def test_prices_are_vat_inclusive(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    load_cfc(cl, M)
    st = cl.get("/api/state", headers=M).json()["settings"]
    assert (st["tax_rate_pct"], st["tax_inclusive"]) == ("16", "1")
    open_day(cl, M, date="2026-10-28")
    open_shift(cl, C, 0)
    menu = items(cl, C)
    o = sell(cl, C, [(menu["Butter chicken"]["id"], 1), (menu["With – Pilau rice"]["id"], 1), (menu["Soda"]["id"], 2)],
             "cash", amount_tendered=1000).json()
    assert o["total"] == 1000 and o["subtotal"] == 1000        # menu price is what the customer pays: no VAT added on top
    assert o["tax"] == 137.93                                    # VAT shown as the included portion: 1000 × 16/116
    assert o["change"] == 0
    names = [i["name"] for i in o["items"]]
    assert "With – Pilau rice" in names


def test_sale_days_and_hidden_items(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    load_cfc(cl, M)
    open_day(cl, M, date="2026-10-28")  # a Wednesday
    open_shift(cl, C, 0)
    menu = items(cl, C)
    r = sell(cl, C, [(menu["Thursday special – 2 large pizzas"]["id"], 1)])
    assert r.status_code == 400 and "only sold on Thu" in r.json()["detail"]
    with tx() as c:
        hidden = c.execute("SELECT id FROM menu_items WHERE name = 'Black iced'").fetchone()["id"]
    assert sell(cl, C, [(hidden, 1)]).status_code == 400


def test_thursday_special_on_thursday(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    load_cfc(cl, M)
    open_day(cl, M, date="2026-10-29")  # a Thursday
    open_shift(cl, C, 0)
    menu = items(cl, C)
    o = sell(cl, C, [(menu["Thursday special – 2 medium pizzas"]["id"], 1)], note=None)
    assert o.status_code == 200 and o.json()["total"] == 900


def test_line_notes_keep_lines_apart(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    load_cfc(cl, M)
    open_day(cl, M, date="2026-10-28")
    open_shift(cl, C, 0)
    bc = items(cl, C)["Beef burger"]["id"]
    r = cl.post("/api/orders", headers=C, json={"items": [{"menu_item_id": bc, "qty": 1, "note": "No onions"},
                                                          {"menu_item_id": bc, "qty": 2}], "payment_method": "card", "client_ref": ref()}).json()
    assert sorted((i["qty"], i["note"]) for i in r["items"]) == [(1, "No onions"), (2, None)] and r["total"] == 1740


def test_open_check_full_flow(cl):
    M, C, A = login(cl, "2222"), login(cl, "1111"), login(cl, "1234")
    load_cfc(cl, M)
    open_day(cl, M, date="2026-10-28")
    menu = items(cl, C)
    # a server opens a check without a cash shift
    t = cl.post("/api/tabs", headers=C, json={"label": "T5", "order_type": "Dine in", "guests": 3, "client_ref": ref()}).json()
    assert cl.post("/api/tabs", headers=C, json={"label": "t5"}).status_code == 400  # one open check per table
    batch = ref()
    body = {"items": [{"menu_item_id": menu["Lamb mandi"]["id"], "qty": 2, "note": "1 well done"},
                      {"menu_item_id": menu["Fresh juice – Passion"]["id"], "qty": 3}], "send": True, "batch_ref": batch}
    t = cl.post(f"/api/tabs/{t['id']}/items", headers=C, json=body).json()
    again = cl.post(f"/api/tabs/{t['id']}/items", headers=C, json=body).json()  # retried send: no duplicates
    assert t["total"] == again["total"] == 2 * 900 + 3 * 250
    t = cl.post(f"/api/tabs/{t['id']}/items", headers=C, json={"items": [{"menu_item_id": menu["Soda"]["id"], "qty": 1}], "send": False}).json()
    unsent = next(l for l in t["lines"] if not l["sent_at"])
    sent = next(l for l in t["lines"] if l["sent_at"] and l["qty"] == 3)
    assert cl.post(f"/api/tab-lines/{unsent['id']}/remove", headers=C, json={}).status_code == 200      # not sent: server may remove
    r = cl.post(f"/api/tab-lines/{sent['id']}/remove", headers=C, json={"reason": "Customer changed mind"})
    assert r.status_code == 400 and "manager" in r.json()["detail"]
    t = cl.post(f"/api/tab-lines/{sent['id']}/remove", headers=C, json={"reason": "Customer changed mind", "approver_pin": "2222"}).json()
    assert t["total"] == 1800
    # stock does not move and nothing is sold until payment
    assert cl.get("/api/orders", headers=C).json() == []
    # the day can't close with an open check
    r = cl.post("/api/day/close", headers=M, json={"counts": {}})
    assert r.status_code == 400 and "T5" in r.json()["detail"]
    # cashier takes payment (needs a shift); idempotent
    open_shift(cl, C, 500)
    key = ref()
    o = cl.post(f"/api/tabs/{t['id']}/pay", headers=C, json={"payment_method": "mpesa", "payment_ref": "QWE1234567", "client_ref": key}).json()
    o2 = cl.post(f"/api/tabs/{t['id']}/pay", headers=C, json={"payment_method": "mpesa", "payment_ref": "QWE1234567", "client_ref": key}).json()
    assert o["id"] == o2["id"] and o2["duplicate"] and o["total"] == 1800 and o["tab_label"] == "T5" and o["guests"] == 3
    assert o["items"][0]["note"] == "1 well done"
    assert cl.get(f"/api/tabs/{t['id']}", headers=C).json()["status"] == "paid"
    assert cl.get("/api/tabs", headers=C).json() == []
    day = cl.get("/api/state", headers=M).json()["day"]["business_date"]
    rep = cl.get("/api/reports/run/voids", headers=M, params={"date_from": day, "date_to": day}).json()
    iv = next(s for s in rep["sections"] if s["title"].startswith("Item voids"))
    assert iv["rows"][0]["item"] == "3× Fresh juice – Passion" and iv["rows"][0]["approved"] == "Manager Demo" and iv["totals"]["value"] == 750
    emp = cl.get("/api/reports/run/employee", headers=A, params={"date_from": day, "date_to": day}).json()
    assert emp["sections"][0]["rows"][0]["employee"] == "Cashier Demo" and emp["sections"][0]["rows"][0]["checks"] == 1


def test_check_paid_by_other_cashier_credits_server(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    load_cfc(cl, M)
    open_day(cl, M, date="2026-10-28")
    menu = items(cl, C)
    t = cl.post("/api/tabs", headers=C, json={"label": "Bar 1"}).json()
    cl.post(f"/api/tabs/{t['id']}/items", headers=C, json={"items": [{"menu_item_id": menu["Soda"]["id"], "qty": 2}]})
    open_shift(cl, M, 0)
    o = cl.post(f"/api/tabs/{t['id']}/pay", headers=M, json={"payment_method": "card", "client_ref": ref()}).json()
    assert o["cashier"] == "Manager Demo" and o["server"] == "Cashier Demo"
    day = cl.get("/api/state", headers=M).json()["day"]["business_date"]
    emp = cl.get("/api/reports/run/employee", headers=M, params={"date_from": day, "date_to": day}).json()
    assert [r["employee"] for r in emp["sections"][0]["rows"]] == ["Cashier Demo"]
    sh = cl.get("/api/reports/run/cashier", headers=M, params={"date_from": day, "date_to": day}).json()
    assert next(r for r in sh["sections"][0]["rows"] if r["cashier"] == "Manager Demo")["card"] == 200


def test_cancel_check_rules(cl):
    M, C = login(cl, "2222"), login(cl, "1111")
    load_cfc(cl, M)
    open_day(cl, M, date="2026-10-28")
    menu = items(cl, C)
    t = cl.post("/api/tabs", headers=C, json={"label": "T9"}).json()
    assert cl.post(f"/api/tabs/{t['id']}/pay", headers=C, json={"payment_method": "card"}).status_code == 400  # empty check
    cl.post(f"/api/tabs/{t['id']}/items", headers=C, json={"items": [{"menu_item_id": menu["Chapati"]["id"], "qty": 2}]})
    assert cl.post(f"/api/tabs/{t['id']}/cancel", headers=C, json={"reason": "left"}).status_code == 400
    r = cl.post(f"/api/tabs/{t['id']}/cancel", headers=M, json={"reason": "Customer left"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert cl.post(f"/api/tabs/{t['id']}/items", headers=C, json={"items": [{"menu_item_id": menu["Chapati"]["id"], "qty": 1}]}).status_code == 400
    # an empty check can be cancelled by the server without approval
    t2 = cl.post("/api/tabs", headers=C, json={"label": "T9"}).json()
    assert cl.post(f"/api/tabs/{t2['id']}/cancel", headers=C, json={}).status_code == 200


def test_setup_checklist(cl):
    M = login(cl, "2222")
    st = cl.get("/api/setup/status", headers=M).json()
    keys = {i["key"]: i["done"] for i in st["items"]}
    assert keys["menu"] and not keys["tax"] and not keys["backup"]
    load_cfc(cl, M)
    st = cl.get("/api/setup/status", headers=M).json()
    keys = {i["key"]: i["done"] for i in st["items"]}
    assert keys["tax"] and keys["business"] and not keys["flags"] and len(st["flagged_items"]) == 3
