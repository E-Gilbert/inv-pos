"""Shared test setup: a throw-away database and secret per test session, never data/cfc.db."""
import os
import tempfile
import uuid

_tmp = tempfile.mkdtemp(prefix="cfc-test-")
os.environ["CFC_DB"] = os.path.join(_tmp, "test.db")
os.environ.pop("CFC_LICENCE_MODE", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.security import throttle  # noqa: E402
from app.seed import seed  # noqa: E402

DEV_KEY = open(os.path.join(os.path.dirname(__file__), "dev_licence_key.txt")).read().split()[-1]


@pytest.fixture()
def cl():
    """Fresh demo database for every test."""
    seed(reset=True, force=True)
    throttle.reset()
    with TestClient(app) as c:
        yield c


def login(cl, pin):
    r = cl.post("/api/login", json={"pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Token": r.json()["token"], "X-Terminal": "test-" + pin}


def ref():
    return uuid.uuid4().hex


def open_day(cl, M, date="2026-01-01", qty_g=20000, qty_pcs=48):
    rows = cl.get("/api/day/opening-preview", headers=M).json()["rows"]
    counts = {str(i["id"]): (qty_pcs if i["unit"] == "pcs" else qty_g) for i in rows}
    r = cl.post("/api/day/open", headers=M, json={"business_date": date, "opening_counts": counts})
    assert r.status_code == 200, r.text


def open_shift(cl, h, float_=0):
    r = cl.post("/api/shifts/open", headers=h, json={"opening_float": float_})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def menu_ids(cl, h):
    return {i["name"]: i["id"] for i in cl.get("/api/menu", headers=h).json()["items"]}


def ing_id(cl, h, name):
    return next(i["id"] for i in cl.get("/api/ingredients", headers=h).json() if i["name"] == name)


def sell(cl, h, lines, method="card", **kw):
    body = {"items": [{"menu_item_id": m, "qty": q} for m, q in lines], "payment_method": method, "client_ref": ref()}
    body.update(kw)
    return cl.post("/api/orders", headers=h, json=body)
