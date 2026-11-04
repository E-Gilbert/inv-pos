"""Cloud mode: inventory-only switch, website (Supabase) logins, staff e-mail, SQL translation."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import app.webauth as webauth
from app.pg import translate
from conftest import ing_id, login, open_day

SB = "https://demo-project.supabase.co"
HS_SECRET = "test-legacy-secret-that-is-long-enough-1234567890"


def token(email="mgr@cityfoodcourt.co.ke", key=HS_SECRET, alg="HS256", **over):
    claims = {"sub": "0f6c1d2e-0000-4000-8000-000000000001", "aud": "authenticated", "iss": SB + "/auth/v1",
              "role": "authenticated", "email": email, "exp": int(time.time()) + 3600, "iat": int(time.time())}
    claims.update(over)
    return jwt.encode(claims, key, algorithm=alg)


@pytest.fixture()
def web(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SB)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", HS_SECRET)


def link_email(cl, A, name, email):
    sid = next(s["id"] for s in cl.get("/api/staff", headers=A).json() if s["name"] == name)
    s = next(s for s in cl.get("/api/staff", headers=A).json() if s["id"] == sid)
    return cl.put(f"/api/staff/{sid}", headers=A, json={"name": s["name"], "role": s["role"], "active": True, "email": email})


# ---------------------------------------------------------------- inventory-only mode

def test_pos_module_off_hides_till_but_keeps_inventory(cl, monkeypatch):
    monkeypatch.setenv("POS_MODULE", "off")
    M, C = login(cl, "2222"), login(cl, "1111")
    st = cl.get("/api/state", headers=M).json()
    assert st["modules"] == {"pos": False}
    assert not any(p.startswith(("pos.", "shift.")) for p in st["permissions"])
    assert cl.get("/api/health").json()["pos"] is False
    open_day(cl, M)
    # till endpoints are gone, for every role
    r = cl.post("/api/orders", headers=C, json={"items": [{"menu_item_id": 1, "qty": 1}], "payment_method": "cash"})
    assert r.status_code == 404 and "inventory-only" in r.json()["detail"]
    assert cl.get("/api/tabs", headers=M).status_code == 404
    assert cl.post("/api/shifts/open", headers=C, json={"opening_float": 0}).status_code == 404
    # inventory still works
    rice = ing_id(cl, M, "Basmati rice")
    assert cl.post("/api/stock/movement", headers=M, json={"ingredient_id": rice, "kind": "waste", "qty": 100,
                                                            "reason": "Spoiled"}).status_code == 200
    assert cl.get("/api/stock/overview", headers=M).status_code == 200
    assert cl.get("/api/suppliers", headers=M).status_code == 200
    assert cl.get("/api/purchase-orders", headers=M).status_code == 200


def test_pos_module_on_by_default(cl):
    M = login(cl, "2222")
    assert cl.get("/api/state", headers=M).json()["modules"] == {"pos": True}


# ---------------------------------------------------------------- website logins

def test_staff_email_link_is_unique_and_optional(cl):
    A = login(cl, "1234")
    assert link_email(cl, A, "Manager Demo", " Mgr@CityFoodCourt.co.ke ").status_code == 200
    staff = {s["name"]: s for s in cl.get("/api/staff", headers=A).json()}
    assert staff["Manager Demo"]["email"] == "mgr@cityfoodcourt.co.ke"
    r = link_email(cl, A, "Cashier Demo", "MGR@cityfoodcourt.co.ke")  # same address, different case
    assert r.status_code == 400 and "already linked" in r.json()["detail"]
    assert link_email(cl, A, "Cashier Demo", "not-an-email").status_code == 422
    # editing without the field keeps the link; "" removes it
    sid = staff["Manager Demo"]["id"]
    assert cl.put(f"/api/staff/{sid}", headers=A, json={"name": "Manager Demo", "role": "manager", "active": True}).status_code == 200
    assert {s["name"]: s for s in cl.get("/api/staff", headers=A).json()}["Manager Demo"]["email"] == "mgr@cityfoodcourt.co.ke"
    assert link_email(cl, A, "Manager Demo", "").status_code == 200
    assert {s["name"]: s for s in cl.get("/api/staff", headers=A).json()}["Manager Demo"]["email"] is None


def test_web_login_maps_to_staff_role(cl, web):
    A = login(cl, "1234")
    link_email(cl, A, "Manager Demo", "mgr@cityfoodcourt.co.ke")
    h = {"Authorization": "Bearer " + token()}
    st = cl.get("/api/state", headers=h).json()
    assert st["staff"]["name"] == "Manager Demo" and st["staff"]["role"] == "manager"
    assert cl.get("/api/reports/catalog", headers=h).status_code == 200      # manager permission
    assert cl.get("/api/staff", headers=h).status_code == 403                # admin-only, still enforced


def test_web_login_rejections(cl, web):
    A = login(cl, "1234")
    link_email(cl, A, "Manager Demo", "mgr@cityfoodcourt.co.ke")
    bad = {
        "unknown e-mail": token(email="someone@else.com"),
        "expired": token(exp=int(time.time()) - 3600),
        "wrong secret": token(key="x" * 48),
        "wrong issuer": token(iss="https://evil.supabase.co/auth/v1"),
        "wrong audience": token(aud="anon"),
        "anonymous user": token(is_anonymous=True),
        "no e-mail": token(email=""),
        "unsigned": jwt.encode({"email": "mgr@cityfoodcourt.co.ke"}, None, algorithm="none"),
    }
    for why, t in bad.items():
        r = cl.get("/api/state", headers={"Authorization": "Bearer " + t})
        assert r.status_code in (401, 403), why
    assert cl.get("/api/state", headers={"Authorization": "Basic abc"}).status_code == 401
    # a disabled staff member loses website access too
    sid = next(s["id"] for s in cl.get("/api/staff", headers=A).json() if s["name"] == "Manager Demo")
    cl.put(f"/api/staff/{sid}", headers=A, json={"name": "Manager Demo", "role": "manager", "active": False})
    assert cl.get("/api/state", headers={"Authorization": "Bearer " + token()}).status_code == 403


def test_web_login_off_without_supabase_url(cl, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert cl.get("/api/state", headers={"Authorization": "Bearer " + token()}).status_code == 401


def test_web_login_asymmetric_key(cl, web, monkeypatch):
    """New Supabase projects sign with ES256; the public key comes from the project's JWKS."""
    priv = ec.generate_private_key(ec.SECP256R1())

    class FakeJWKS:
        def get_signing_key_from_jwt(self, _t):
            return type("K", (), {"key": priv.public_key()})()

    monkeypatch.setattr(webauth, "_jwks_client", FakeJWKS())
    monkeypatch.setattr(webauth, "_jwks_url", SB + "/auth/v1/.well-known/jwks.json")
    A = login(cl, "1234")
    link_email(cl, A, "Manager Demo", "mgr@cityfoodcourt.co.ke")
    t = token(key=priv, alg="ES256")
    assert cl.get("/api/state", headers={"Authorization": "Bearer " + t}).json()["staff"]["role"] == "manager"
    other = ec.generate_private_key(ec.SECP256R1())
    assert cl.get("/api/state", headers={"Authorization": "Bearer " + token(key=other, alg="ES256")}).status_code == 401


# ---------------------------------------------------------------- SQL translation (Postgres backend)

def test_sql_translation():
    assert translate("SELECT * FROM staff WHERE id = ? AND name = '?'") == (
        "SELECT * FROM staff WHERE id = %s AND name = '?'", False)
    assert translate("INSERT OR IGNORE INTO categories(name) VALUES (?)") == (
        "INSERT INTO categories(name) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id", True)
    assert translate("INSERT OR IGNORE INTO day_stock(day_id, ingredient_id) VALUES (?, ?)")[0].endswith(
        "ON CONFLICT DO NOTHING")
    assert translate("SELECT 1 FROM tabs WHERE label = ? COLLATE NOCASE")[0] == "SELECT 1 FROM tabs WHERE label = %s"
    q = translate("SELECT GROUP_CONCAT(qty || '× ' || name, ', ') FROM t WHERE x LIKE ?")[0]
    assert q == "SELECT STRING_AGG((qty || '× ' || name)::text, ', ') FROM t WHERE x LIKE %s"
    assert translate("SELECT 'a%b' FROM t")[0] == "SELECT 'a%%b' FROM t"
    assert translate("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value") == (
        "INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value", False)


def test_cloud_secret_from_environment(monkeypatch):
    import app.security as sec
    saved = sec._secret
    try:
        monkeypatch.setattr(sec, "_secret", None)
        monkeypatch.setenv("INVPOS_SECRET", "ef" * 32)
        assert sec.secret() == bytes.fromhex("ef" * 32)
        for bad in ("zz" * 32, "ab" * 8):
            monkeypatch.setattr(sec, "_secret", None)
            monkeypatch.setenv("INVPOS_SECRET", bad)
            with pytest.raises(RuntimeError):
                sec.secret()
        # cloud mode without the variable must refuse rather than invent a file that dies on redeploy
        monkeypatch.setattr(sec, "_secret", None)
        monkeypatch.delenv("INVPOS_SECRET")
        monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
        with pytest.raises(RuntimeError, match="INVPOS_SECRET"):
            sec.secret()
    finally:
        sec._secret = saved
