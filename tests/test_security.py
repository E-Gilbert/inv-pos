"""Authentication, sessions, permissions, throttling, headers, migration of v0.1 data."""
import os
import sqlite3

import app.db as db
from app.security import hash_pin, hash_token
from conftest import ing_id, login, menu_ids, open_day, sell


def test_pins_are_not_stored_in_plain_text(cl):
    con = sqlite3.connect(os.environ["CFC_DB"])
    cols = [r[1] for r in con.execute("PRAGMA table_info(staff)")]
    assert "pin" not in cols and "pin_hash" in cols
    hashes = [r[0] for r in con.execute("SELECT pin_hash FROM staff")]
    assert "1234" not in hashes and hash_pin("1234") in hashes
    t = login(cl, "1111")["X-Token"]
    tokens = [r[0] for r in con.execute("SELECT token_hash FROM sessions")]
    assert t not in tokens and hash_token(t) in tokens


def test_wrong_pin_throttled(cl):
    for _ in range(5):
        assert cl.post("/api/login", json={"pin": "9999"}).status_code == 401
    r = cl.post("/api/login", json={"pin": "1111"})  # even the right PIN waits during lockout
    assert r.status_code == 429


def test_unauthenticated_and_bad_tokens(cl):
    assert cl.get("/api/state").status_code == 401
    assert cl.get("/api/state", headers={"X-Token": "nope"}).status_code == 401
    assert cl.get("/api/state", headers={"X-Token": "' OR 1=1 --"}).status_code == 401
    h = login(cl, "1111")
    assert cl.post("/api/logout", headers=h).status_code == 200
    assert cl.get("/api/state", headers=h).status_code == 401


def test_session_expiry(cl):
    h = login(cl, "1111")
    con = sqlite3.connect(os.environ["CFC_DB"])
    # activity slides the idle timeout forward, capped at 24 h after login
    con.execute("UPDATE sessions SET expires_at = datetime('now','localtime','+1 minute')")
    con.commit()
    assert cl.get("/api/state", headers=h).status_code == 200
    assert con.execute("SELECT expires_at > datetime('now','localtime','+60 minutes') FROM sessions").fetchone()[0] == 1
    con.execute("UPDATE sessions SET created_at = datetime('now','localtime','-25 hours')")
    con.commit()
    cl.get("/api/state", headers=h)  # recomputed expiry is already in the past (hard cap)
    assert cl.get("/api/state", headers=h).status_code == 401
    h = login(cl, "1111")
    con.execute("UPDATE sessions SET expires_at = '2000-01-01 00:00:00'")
    con.commit()
    assert cl.get("/api/state", headers=h).status_code == 401


def test_permission_matrix(cl):
    C, S, M, A = login(cl, "1111"), login(cl, "3333"), login(cl, "2222"), login(cl, "1234")
    open_day(cl, M)
    rice = ing_id(cl, M, "Basmati rice")
    # cashier: no costs, no void/adjust/receive/export/audit/staff/settings
    assert "cost_per_unit" not in cl.get("/api/ingredients", headers=C).json()[0]
    assert cl.post("/api/stock/movement", headers=C, json={"ingredient_id": rice, "kind": "adjustment", "qty": 5, "reason": "Other"}).status_code == 403
    assert cl.post("/api/stock/movement", headers=C, json={"ingredient_id": rice, "kind": "delivery", "qty": 5}).status_code == 403
    for path in ("/api/export/stock.csv", "/api/audit", "/api/staff", "/api/reports/valuation", "/api/suppliers", "/api/licence"):
        assert cl.get(path, headers=C).status_code == 403, path
    assert cl.put("/api/settings", headers=M, json={"business_name": "X", "variance_tolerance_pct": 2}).status_code == 403
    # stock controller: receives and purchases, can't sell or adjust
    assert cl.post("/api/stock/movement", headers=S, json={"ingredient_id": rice, "kind": "delivery", "qty": 5}).status_code == 200
    assert cl.get("/api/suppliers", headers=S).status_code == 200
    assert sell(cl, S, [(menu_ids(cl, S)["Kachumbari"], 1)]).status_code == 403
    assert cl.post("/api/stock/movement", headers=S, json={"ingredient_id": rice, "kind": "adjustment", "qty": 5, "reason": "Other"}).status_code == 403
    # manager adjusts, admin sees audit
    assert cl.post("/api/stock/movement", headers=M, json={"ingredient_id": rice, "kind": "adjustment", "qty": -5, "reason": "Counting error corrected"}).status_code == 200
    acts = [a["action"] for a in cl.get("/api/audit", headers=A).json()]
    assert "stock.adjust" in acts and "day.open" in acts


def test_first_run_admin_must_change_pin(cl, monkeypatch):
    con = sqlite3.connect(os.environ["CFC_DB"])
    con.execute("UPDATE staff SET must_change_pin = 1 WHERE role = 'admin'")
    con.commit()
    A = login(cl, "1234")
    assert cl.get("/api/staff", headers=A).status_code == 403
    assert cl.get("/api/state", headers=A).json()["staff"]["must_change_pin"]
    assert cl.post("/api/me/pin", headers=A, json={"current_pin": "1234", "new_pin": "5678"}).status_code == 400  # sequential
    assert cl.post("/api/me/pin", headers=A, json={"current_pin": "0000", "new_pin": "4826"}).status_code == 400  # wrong current
    assert cl.post("/api/me/pin", headers=A, json={"current_pin": "1234", "new_pin": "4826"}).status_code == 200
    assert cl.get("/api/staff", headers=A).status_code == 200
    assert cl.post("/api/login", json={"pin": "1234"}).status_code == 401


def test_last_admin_protected_and_role_change_logs_out(cl):
    A, C = login(cl, "1234"), login(cl, "1111")
    admin = next(s for s in cl.get("/api/staff", headers=A).json() if s["role"] == "admin")
    r = cl.put(f"/api/staff/{admin['id']}", headers=A, json={"name": "Admin", "role": "manager", "active": True})
    assert r.status_code == 400
    cashier = next(s for s in cl.get("/api/staff", headers=A).json() if s["role"] == "cashier")
    cl.put(f"/api/staff/{cashier['id']}", headers=A, json={"name": "Cashier Demo", "role": "cashier", "active": False})
    assert cl.get("/api/state", headers=C).status_code == 401
    # weak PINs refused for managers; reused PIN refused
    assert cl.post("/api/staff", headers=A, json={"name": "M2", "pin": "4444", "role": "manager"}).status_code == 400
    assert cl.post("/api/staff", headers=A, json={"name": "C2", "pin": "2222", "role": "cashier"}).status_code == 400


def test_security_headers_and_errors(cl):
    r = cl.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    h = login(cl, "2222")
    r = cl.post("/api/orders", headers=h, json={"items": "bad"})
    assert r.status_code == 422 and "Check the form" in r.json()["detail"]
    big = b"x" * (7 * 1024 * 1024)
    assert cl.post("/api/excel", headers=h, files={"file": ("a.xlsx", big)}).status_code == 413
    assert cl.get("/api/orders/search", headers=h, params={"q": "'; DROP TABLE orders; --"}).status_code == 200
    assert cl.get("/api/days", headers=h).status_code == 200


def test_xss_payload_is_stored_as_text(cl):
    M = login(cl, "2222")
    name = '<img src=x onerror=alert(1)>'
    assert cl.post("/api/ingredients", headers=M, json={"name": name, "unit": "pcs"}).status_code == 200
    assert any(i["name"] == name for i in cl.get("/api/ingredients", headers=M).json())
    js = open(os.path.join(os.path.dirname(__file__), "..", "static", "app.js"), encoding="utf-8").read()
    assert "const esc =" in js  # UI escapes all server text before inserting HTML


def test_migration_from_v01_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(db.SCHEMA_V1)
    con.executescript("""
        INSERT INTO staff(id, name, pin, role) VALUES (1,'Admin','1234','admin'),(2,'Jane','8361','cashier'),(3,'Mo','2222','manager');
        INSERT INTO ingredients(id, name, unit, cost_per_unit) VALUES (1,'Rice','g',0.25);
        INSERT INTO menu_items(id, name, price) VALUES (1,'Pilau',500);
        INSERT INTO days(id, business_date, status) VALUES (1,'2026-10-01','closed');
        INSERT INTO orders(id, day_id, number, staff_id, total, payment_method) VALUES (1,1,1,2,500,'cash');
        INSERT INTO order_items(order_id, menu_item_id, name, price, qty) VALUES (1,1,'Pilau',500,1);
        INSERT INTO stock_movements(day_id, ingredient_id, type, qty, order_id, staff_id) VALUES (1,1,'sale',-200,1,2);
        INSERT INTO day_stock(day_id, ingredient_id, opening, counted) VALUES (1,1,1000,800);
        INSERT INTO sessions(token, staff_id) VALUES ('abc', 1);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    db.init_db()
    db.init_db()  # idempotent
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    staff = {r["name"]: r for r in con.execute("SELECT * FROM staff")}
    assert staff["Jane"]["pin_hash"] == hash_pin("8361") and staff["Jane"]["must_change_pin"] == 0
    assert staff["Admin"]["must_change_pin"] == 1 and staff["Mo"]["must_change_pin"] == 1  # weak manager/admin PINs
    assert con.execute("SELECT qty FROM stock_movements").fetchone()[0] == -200
    assert con.execute("SELECT subtotal FROM orders").fetchone()[0] == 500
    assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert any(f.startswith("pre-migration-v0") for f in os.listdir(tmp_path / "backups"))
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
