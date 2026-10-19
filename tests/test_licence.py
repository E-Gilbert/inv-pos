"""Licence verification, development mode, enforcement, grace, revocation, device limits."""
import base64
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import licence
from conftest import DEV_KEY, login, menu_ids, open_day, open_shift, sell


def _lic(key=DEV_KEY, days=30, **kw):
    p = {"lid": "L-test", "customer": "Test", "issued": date.today().isoformat(),
         "expires": (date.today() + timedelta(days=days)).isoformat(), "grace_days": 7} | kw
    return licence.sign(p, key)


@pytest.fixture()
def prod_key(monkeypatch):
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    monkeypatch.setenv("CFC_LICENCE_PUBKEY", pub)
    monkeypatch.setenv("CFC_LICENCE_MODE", "enforce")
    return priv


def test_development_mode_never_blocks(cl):
    A, M, C = login(cl, "1234"), login(cl, "2222"), login(cl, "1111")
    st = cl.get("/api/licence", headers=A).json()
    assert st["mode"] == "development" and not st["enforced"] and st["state"] == "unlicensed"
    open_day(cl, M)
    open_shift(cl, C)
    assert sell(cl, C, [(menu_ids(cl, C)["Kachumbari"], 1)]).status_code == 200


def test_dev_licence_install_and_expiry_states(cl):
    A = login(cl, "1234")
    assert cl.post("/api/licence", headers=A, json={"token": "garbage.token"}).status_code == 400
    tampered = _lic()[:-4] + "AAAA"
    assert cl.post("/api/licence", headers=A, json={"token": tampered}).status_code == 400
    assert cl.post("/api/licence", headers=A, json={"token": _lic(days=10)}).json()["state"] == "valid"
    assert cl.post("/api/licence", headers=A, json={"token": _lic(days=-3)}).json()["state"] == "grace"
    assert cl.post("/api/licence", headers=A, json={"token": _lic(days=-30)}).json()["state"] == "expired"
    assert cl.get("/api/licence", headers=login(cl, "2222")).status_code == 403


def test_enforce_mode_rejects_dev_key(cl, monkeypatch):
    monkeypatch.setenv("CFC_LICENCE_MODE", "enforce")
    A = login(cl, "1234")
    r = cl.post("/api/licence", headers=A, json={"token": _lic()})
    assert r.status_code == 400 and "production public key" in r.json()["detail"]


def test_enforce_blocks_new_sales_but_not_records(cl, prod_key):
    A, M, C = login(cl, "1234"), login(cl, "2222"), login(cl, "1111")
    # unlicensed: can't open a day / sell, can still read reports and export
    r = cl.post("/api/day/open", headers=M, json={"business_date": "2026-01-01"})
    assert r.status_code == 402
    assert cl.get("/api/days", headers=M).status_code == 200
    assert cl.get("/api/export/stock.csv", headers=M).status_code == 200
    cl.post("/api/licence", headers=A, json={"token": _lic(prod_key, days=-3)})  # in grace: still works
    open_day(cl, M)
    open_shift(cl, C)
    menu = menu_ids(cl, C)
    assert sell(cl, C, [(menu["Kachumbari"], 1)]).status_code == 200
    cl.post("/api/licence", headers=A, json={"token": _lic(prod_key, days=-30)})  # past grace
    assert sell(cl, C, [(menu["Kachumbari"], 1)]).status_code == 402
    # closing the shift and the day still works so the books can be completed
    sh = cl.get("/api/state", headers=C).json()["shift"]
    assert cl.post(f"/api/shifts/{sh['id']}/close", headers=C, json={"counted_cash": 0}).status_code == 200
    # renewal restores selling
    cl.post("/api/licence", headers=A, json={"token": _lic(prod_key, days=365)})
    open_shift(cl, C)
    assert sell(cl, C, [(menu["Kachumbari"], 1)]).status_code == 200


def test_revocation_and_device_limit(cl, prod_key):
    A, M = login(cl, "1234"), login(cl, "2222")
    cl.post("/api/licence", headers=A, json={"token": _lic(prod_key, max_devices=1)})
    open_day(cl, M)
    C1 = login(cl, "1111")  # terminal "test-1111"
    open_shift(cl, C1)
    menu = menu_ids(cl, C1)
    other = dict(M)
    other["X-Terminal"] = "second-till"
    open_shift(cl, other)
    # the first device to sell is licensed; a second selling device is refused
    assert sell(cl, C1, [(menu["Kachumbari"], 1)]).status_code == 200
    assert sell(cl, other, [(menu["Kachumbari"], 1)]).status_code == 402
    assert sell(cl, C1, [(menu["Kachumbari"], 1)]).status_code == 200
    # deactivating the old till frees the slot
    assert cl.put("/api/devices/test-1111", headers=A, json={"active": False}).status_code == 200
    assert sell(cl, other, [(menu["Kachumbari"], 1)]).status_code == 200
    till = other
    rev = licence.sign({"revoked": ["L-test"], "issued": date.today().isoformat()}, prod_key)
    assert cl.post("/api/licence/revocations", headers=A, json={"token": rev}).json()["status"]["state"] == "revoked"
    assert sell(cl, till, [(menu["Kachumbari"], 1)]).status_code == 402
    forged = licence.sign({"revoked": ["x"]}, DEV_KEY)
    assert cl.post("/api/licence/revocations", headers=A, json={"token": forged}).status_code == 400
