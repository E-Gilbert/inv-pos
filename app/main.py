"""CFC INV-POS — HTTP API and static UI.

Run:  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Then open http://<server-ip>:8000 on any till, tablet or PC on the restaurant network.

Security model (summary — see SECURITY.md):
* Staff log in with a PIN; the server issues a random session token (stored hashed) that the
  browser sends in the X-Token header. Sessions expire after inactivity.
* Every endpoint checks the role permission on the SERVER (security.PERMISSIONS).
* Logins are throttled; sensitive actions are written to the audit log.
"""
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import importer, licence, logic, reports, workbook
from .db import BASE_DIR, DATA_DIR, DEFAULT_SETTINGS, audit, get_setting, init_db, now, setting_float, tx
from .logic import Conflict, RuleError
from .security import (allowed, hash_pin, hash_token, new_token, permissions_for, throttle,
                       valid_pin_format, weak_pin)

VERSION = "0.3.0"
STATIC = os.path.join(BASE_DIR, "static")
MAX_UPLOAD = 5 * 1024 * 1024
MAX_BODY = 6 * 1024 * 1024
log = logging.getLogger("cfc")


def first_run():
    with tx() as c:
        if not c.execute("SELECT 1 FROM staff").fetchone():
            # One admin so someone can log in. The PIN must be changed at first login.
            c.execute("INSERT INTO staff(name, pin_hash, role, must_change_pin) VALUES ('Admin', ?, 'admin', 1)",
                      (hash_pin("1234"),))
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))


@asynccontextmanager
async def lifespan(_app):
    init_db()
    first_run()
    yield


app = FastAPI(title="CFC INV-POS", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- middleware / errors

@app.middleware("http")
async def guard(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY:
        return JSONResponse(status_code=413, content={"detail": "File or request too large."})
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(RuleError)
def rule_error(_: Request, exc: RuleError):
    return JSONResponse(status_code=409 if isinstance(exc, Conflict) else 400, content={"detail": str(exc)})


@app.exception_handler(importer.ImportErrors)
def import_errors(_: Request, exc: importer.ImportErrors):
    return JSONResponse(status_code=400, content={"detail": str(exc), "errors": exc.errors})


@app.exception_handler(ValueError)
def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(licence.LicenceBlocked)
def licence_blocked(_: Request, exc: licence.LicenceBlocked):
    return JSONResponse(status_code=402, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
def validation_error(_: Request, exc: RequestValidationError):
    msgs = []
    for e in exc.errors()[:5]:
        field = ".".join(str(x) for x in e.get("loc", [])[1:]) or "request"
        msgs.append(f"{field}: {e.get('msg')}")
    return JSONResponse(status_code=422, content={"detail": "Check the form: " + "; ".join(msgs)})


@app.exception_handler(Exception)
def unexpected(_: Request, exc: Exception):
    ref = secrets.token_hex(4)
    log.exception("Unhandled error ref=%s", ref)
    return JSONResponse(status_code=500, content={"detail": f"Something went wrong on the server (ref {ref}). Nothing was saved."})


# ---------------------------------------------------------------- auth

def client_ip(request: Request):
    return request.client.host if request.client else "?"


def current_staff(request: Request, x_token: Optional[str] = Header(default=None),
                  x_terminal: Optional[str] = Header(default=None)):
    if not x_token:
        raise HTTPException(401, "Please log in.")
    th = hash_token(x_token)
    with tx() as c:
        row = c.execute(
            """SELECT s.id, s.name, s.role, s.must_change_pin, x.expires_at, x.last_seen FROM sessions x
               JOIN staff s ON s.id = x.staff_id WHERE x.token_hash = ? AND s.active = 1""", (th,)).fetchone()
        if not row or row["expires_at"] < now():
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            raise HTTPException(401, "Session expired. Please log in again.")
        # Sliding idle timeout, but never more than 24 h after login.
        idle = int(setting_float(c, "session_idle_minutes"))
        created = c.execute("SELECT created_at FROM sessions WHERE token_hash = ?", (th,)).fetchone()["created_at"]
        hard = datetime.strptime(created, "%Y-%m-%d %H:%M:%S") + timedelta(hours=24)
        exp = min(datetime.now() + timedelta(minutes=idle), hard)
        c.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                  (now(), exp.strftime("%Y-%m-%d %H:%M:%S"), th))
        if x_terminal and len(x_terminal) <= 64:
            c.execute("""INSERT INTO terminals(id, first_seen, last_seen, user_agent) VALUES (?, ?, ?, ?)
                         ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen""",
                      (x_terminal, now(), now(), (request.headers.get("user-agent") or "")[:200]))
    staff = {"id": row["id"], "name": row["name"], "role": row["role"], "must_change_pin": bool(row["must_change_pin"]),
             "ip": client_ip(request), "terminal": x_terminal if x_terminal and len(x_terminal) <= 64 else None}
    if staff["must_change_pin"] and request.url.path not in ("/api/state", "/api/me/pin", "/api/logout"):
        raise HTTPException(403, "Change your PIN before continuing.")
    return staff


def need(perm):
    def dep(staff=Depends(current_staff)):
        if not allowed(staff["role"], perm):
            raise HTTPException(403, "You don't have permission to do this.")
        return staff
    return dep


class Login(BaseModel):
    pin: str = Field(min_length=1, max_length=12)


@app.post("/api/login")
def login(body: Login, request: Request, x_terminal: Optional[str] = Header(default=None)):
    key = client_ip(request)
    wait = throttle.check(key)
    if wait:
        raise HTTPException(429, f"Too many wrong PINs. Try again in {wait} seconds.")
    with tx() as c:
        s = c.execute("SELECT id, name, role, must_change_pin FROM staff WHERE pin_hash = ? AND active = 1",
                      (hash_pin(body.pin),)).fetchone()
        if not s:
            throttle.fail(key)
            audit(c, None, "auth.fail", ip=key)
            raise HTTPException(401, "Wrong PIN.")
        throttle.success(key)
        tok, th = new_token()
        idle = int(setting_float(c, "session_idle_minutes"))
        exp = min(datetime.now() + timedelta(minutes=idle), datetime.now() + timedelta(hours=24))
        c.execute("INSERT INTO sessions(token_hash, staff_id, created_at, last_seen, expires_at, ip, terminal_id) VALUES (?,?,?,?,?,?,?)",
                  (th, s["id"], now(), now(), exp.strftime("%Y-%m-%d %H:%M:%S"), key, (x_terminal or "")[:64] or None))
        audit(c, s["id"], "auth.login", ip=key)
    return {"token": tok, "staff": {"id": s["id"], "name": s["name"], "role": s["role"],
                                    "must_change_pin": bool(s["must_change_pin"])}}


@app.post("/api/logout")
def logout(x_token: Optional[str] = Header(default=None)):
    with tx() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(x_token or ""),))
    return {"ok": True}


class PinChange(BaseModel):
    current_pin: str = Field(max_length=12)
    new_pin: str = Field(max_length=12)


@app.post("/api/me/pin")
def change_pin(body: PinChange, staff=Depends(current_staff), x_token: Optional[str] = Header(default=None)):
    if not valid_pin_format(body.new_pin):
        raise HTTPException(400, "PIN must be 4–6 digits.")
    if staff["role"] in ("manager", "admin") and weak_pin(body.new_pin):
        raise HTTPException(400, "That PIN is too easy to guess (repeated or sequential digits). Choose another.")
    with tx() as c:
        me = c.execute("SELECT pin_hash FROM staff WHERE id = ?", (staff["id"],)).fetchone()
        if me["pin_hash"] != hash_pin(body.current_pin):
            raise HTTPException(400, "Current PIN is wrong.")
        if c.execute("SELECT 1 FROM staff WHERE pin_hash = ? AND id <> ?", (hash_pin(body.new_pin), staff["id"])).fetchone():
            raise HTTPException(400, "Choose a different PIN.")
        c.execute("UPDATE staff SET pin_hash = ?, must_change_pin = 0 WHERE id = ?", (hash_pin(body.new_pin), staff["id"]))
        c.execute("DELETE FROM sessions WHERE staff_id = ? AND token_hash <> ?", (staff["id"], hash_token(x_token or "")))
        audit(c, staff["id"], "staff.pin_change", "staff", staff["id"], ip=staff["ip"])
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "time": now()}


@app.get("/api/state")
def state(staff=Depends(current_staff)):
    with tx() as c:
        day = logic.open_day_row(c)
        shift = logic.open_shift_for(c, staff["id"])
        lic = licence.status(c)
        return {
            "staff": {k: staff[k] for k in ("id", "name", "role", "must_change_pin")},
            "permissions": permissions_for(staff["role"]),
            "day": dict(day) if day else None,
            "shift": logic.shift_summary(c, shift["id"]) if shift else None,
            "settings": {k: get_setting(c, k) for k in DEFAULT_SETTINGS},
            "business_name": get_setting(c, "business_name"),
            "receipt_footer": get_setting(c, "receipt_footer"),
            "tolerance_pct": setting_float(c, "variance_tolerance_pct"),
            "variance_reasons": logic.VARIANCE_REASONS,
            "waste_reasons": logic.WASTE_REASONS,
            "adjust_reasons": logic.ADJUST_REASONS,
            "refund_reasons": logic.REFUND_REASONS,
            "first_day": not c.execute("SELECT 1 FROM days WHERE status='closed'").fetchone(),
            "licence": {k: lic.get(k) for k in ("mode", "enforced", "state", "message", "days_left", "customer")},
            "version": VERSION,
        }


# ---------------------------------------------------------------- dashboard

@app.get("/api/dashboard")
def dashboard(staff=Depends(need("stock.view"))):
    with tx() as c:
        day = logic.open_day_row(c)
        overview = logic.stock_overview(c)
        counts = {k: 0 for k in logic.STATUS_META}
        for o in overview:
            counts[o["status"]] += 1
        incoming_items = sum(1 for o in overview if o["incoming"] > 0 and o["active"])
        alerts = [o for o in overview if o["status"] in ("out", "critical", "low")]
        alerts.sort(key=lambda o: ("out", "critical", "low").index(o["status"]))
        out = {"day": dict(day) if day else None, "stock_counts": counts, "incoming_items": incoming_items,
               "alerts": alerts[:50]}
        if allowed(staff["role"], "reports.view"):
            out["today"] = logic.sales_summary(c, [day["id"]]) if day else None
            out["open_shifts"] = [logic.shift_summary(c, r["id"]) for r in c.execute("SELECT id FROM shifts WHERE status='open'")]
            out["trend"] = [dict(r) for r in c.execute(
                """SELECT d.business_date, d.source, COALESCE((SELECT SUM(total) FROM orders o WHERE o.day_id=d.id AND o.status='paid'),0)
                          - COALESCE((SELECT SUM(amount) FROM refunds r WHERE r.day_id=d.id),0) AS sales
                   FROM days d ORDER BY d.business_date DESC LIMIT 14""")][::-1]
            out["open_pos"] = c.execute("SELECT COUNT(*) FROM purchase_orders WHERE status IN ('ordered','partial')").fetchone()[0]
            out["valuation"] = logic.money(sum(o["value"] for o in overview if o["active"]))
        return out


# ---------------------------------------------------------------- POS

@app.get("/api/menu")
def menu(staff=Depends(need("stock.view"))):
    with tx() as c:
        cats = [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY sort, name")]
        status = {o["id"]: o["status"] for o in logic.stock_overview(c)}
        items = [dict(r) for r in c.execute(
            """SELECT m.id, m.name, m.price, m.category_id, m.sku, m.barcode,
                      (SELECT COUNT(*) FROM recipe_lines rl WHERE rl.menu_item_id = m.id) AS recipe_lines
               FROM menu_items m WHERE m.active = 1 ORDER BY m.name""")]
        rec = {}
        for r in c.execute("SELECT menu_item_id, ingredient_id FROM recipe_lines"):
            rec.setdefault(r["menu_item_id"], []).append(r["ingredient_id"])
        rank = {"out": 3, "critical": 2, "low": 1}
        for it in items:
            worst = max((rank.get(status.get(i), 0) for i in rec.get(it["id"], [])), default=0)
            it["stock_status"] = {3: "out", 2: "critical", 1: "low"}.get(worst)
    return {"categories": cats, "items": items}


class OrderLine(BaseModel):
    menu_item_id: int
    qty: int = Field(ge=1, le=1000)


class Discount(BaseModel):
    type: Literal["amount", "pct"] = "amount"
    value: float = Field(ge=0, le=10_000_000)
    reason: Optional[str] = Field(default=None, max_length=200)


class NewOrder(BaseModel):
    items: list[OrderLine] = Field(min_length=1, max_length=200)
    payment_method: Literal["cash", "mpesa", "card"]
    amount_tendered: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    payment_ref: Optional[str] = Field(default=None, max_length=40)
    discount: Optional[Discount] = None
    approver_pin: Optional[str] = Field(default=None, max_length=12)
    client_ref: Optional[str] = Field(default=None, min_length=8, max_length=64)
    order_type: Optional[str] = Field(default=None, max_length=30)
    guests: Optional[int] = Field(default=None, ge=0, le=500)


def receipt(c, order_id):
    o = c.execute(
        """SELECT o.*, s.name AS cashier, d.business_date FROM orders o
           JOIN staff s ON s.id = o.staff_id JOIN days d ON d.id = o.day_id WHERE o.id = ?""", (order_id,)).fetchone()
    if not o:
        raise HTTPException(404, "Order not found.")
    out = dict(o)
    out.pop("request_hash", None)
    out["items"] = logic.refundable_lines(c, order_id)
    out["refunds"] = [dict(r) for r in c.execute(
        "SELECT r.id, r.amount, r.method, r.reason, r.restocked, r.created_at FROM refunds r WHERE r.order_id = ? ORDER BY r.id", (order_id,))]
    out["change"] = round((o["amount_tendered"] or 0) - o["total"], 2) if o["payment_method"] == "cash" else 0
    out["tax_label"] = get_setting(c, "tax_label")
    out["business_pin"] = get_setting(c, "business_pin")
    out["business_address"] = get_setting(c, "business_address")
    return out


@app.post("/api/orders")
def new_order(body: NewOrder, staff=Depends(need("pos.sell"))):
    with tx() as c:
        licence.gate(c, "sell", staff["terminal"])
        oid, dup = logic.create_order(
            c, staff, [i.model_dump() for i in body.items], body.payment_method, body.amount_tendered,
            body.payment_ref, body.discount.model_dump() if body.discount else None, body.approver_pin,
            body.client_ref, staff["terminal"], body.order_type, body.guests)
        out = receipt(c, oid)
        out["duplicate"] = dup
        return out


@app.get("/api/orders")
def list_orders(day_id: Optional[int] = None, staff=Depends(need("pos.sell"))):
    with tx() as c:
        if day_id is None:
            day = logic.open_day_row(c)
            if not day:
                return []
            day_id = day["id"]
        return [dict(r) for r in c.execute(
            """SELECT o.id, o.number, o.created_at, o.total, o.discount, o.refunded, o.payment_method, o.payment_ref, o.status,
                      o.void_reason, s.name AS cashier,
                      (SELECT GROUP_CONCAT(qty || '× ' || name, ', ') FROM order_items WHERE order_id = o.id) AS summary
               FROM orders o JOIN staff s ON s.id = o.staff_id
               WHERE o.day_id = ? ORDER BY o.number DESC""", (day_id,))]


@app.get("/api/orders/search")
def search_orders(q: str = Query(min_length=1, max_length=40), staff=Depends(need("pos.refund"))):
    """Find older orders (for refunds): by M-Pesa/card ref, or 'YYYY-MM-DD#number'."""
    with tx() as c:
        if "#" in q:
            d, _, n = q.partition("#")
            rows = c.execute("""SELECT o.id FROM orders o JOIN days d ON d.id = o.day_id
                                WHERE d.business_date = ? AND o.number = ?""", (d.strip(), int(n) if n.strip().isdigit() else -1))
        else:
            rows = c.execute("SELECT id FROM orders WHERE payment_ref = ? ORDER BY id DESC LIMIT 10", (q.strip().upper(),))
        return [receipt(c, r["id"]) for r in rows.fetchall()]


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, staff=Depends(need("pos.sell"))):
    with tx() as c:
        return receipt(c, order_id)


class Void(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


@app.post("/api/orders/{order_id}/void")
def void(order_id: int, body: Void, staff=Depends(need("pos.void"))):
    with tx() as c:
        logic.void_order(c, order_id, staff["id"], body.reason)
        return receipt(c, order_id)


class RefundLine(BaseModel):
    order_item_id: int
    qty: int = Field(ge=0, le=1000)


class Refund(BaseModel):
    lines: list[RefundLine] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=200)
    restock: bool = False
    method: Optional[Literal["cash", "mpesa", "card"]] = None
    approver_pin: Optional[str] = Field(default=None, max_length=12)
    client_ref: Optional[str] = Field(default=None, min_length=8, max_length=64)


@app.post("/api/orders/{order_id}/refund")
def refund(order_id: int, body: Refund, staff=Depends(need("pos.sell"))):
    # A cashier may start a refund, but logic.refund_order requires a manager's PIN unless the user is a manager.
    with tx() as c:
        rid, dup = logic.refund_order(c, staff, order_id, [l.model_dump() for l in body.lines], body.reason, body.restock,
                                      body.method, body.client_ref, body.approver_pin)
        out = receipt(c, order_id)
        out["refund_id"], out["duplicate"] = rid, dup
        return out


# ---------------------------------------------------------------- shifts

class ShiftOpen(BaseModel):
    opening_float: float = Field(ge=0, le=10_000_000)


class ShiftClose(BaseModel):
    counted_cash: float = Field(ge=0, le=10_000_000)
    notes: Optional[str] = Field(default=None, max_length=300)


@app.post("/api/shifts/open")
def shift_open(body: ShiftOpen, staff=Depends(need("shift.own"))):
    with tx() as c:
        sid = logic.open_shift(c, staff["id"], body.opening_float)
        return logic.shift_summary(c, sid)


@app.post("/api/shifts/{shift_id}/close")
def shift_close(shift_id: int, body: ShiftClose, staff=Depends(need("shift.own"))):
    with tx() as c:
        return logic.close_shift(c, shift_id, staff, body.counted_cash, body.notes)


@app.get("/api/shifts")
def shifts(day_id: Optional[int] = None, staff=Depends(need("shift.manage"))):
    with tx() as c:
        if day_id is None:
            day = logic.open_day_row(c)
            ids = [r["id"] for r in c.execute("SELECT id FROM shifts WHERE status='open' OR day_id = ? ORDER BY id",
                                              (day["id"] if day else -1,))]
        else:
            ids = [r["id"] for r in c.execute("SELECT id FROM shifts WHERE day_id = ? ORDER BY id", (day_id,))]
        return [logic.shift_summary(c, i) for i in ids]


# ---------------------------------------------------------------- stock

@app.get("/api/stock")
def stock(staff=Depends(need("stock.view"))):
    with tx() as c:
        day = logic.open_day_row(c)
        if not day:
            return {"day": None, "lines": []}
        return {"day": dict(day), "lines": logic.day_lines(c, day["id"])}


@app.get("/api/stock/overview")
def stock_overview(staff=Depends(need("stock.view"))):
    with tx() as c:
        rows = logic.stock_overview(c)
        if not allowed(staff["role"], "reports.view"):
            for r in rows:  # cashiers don't need cost data
                r.pop("cost_per_unit", None)
                r.pop("value", None)
        return rows


class Movement(BaseModel):
    ingredient_id: int
    kind: Literal["delivery", "waste", "adjustment"]
    qty: float = Field(ge=-1e9, le=1e9)
    reason: Optional[str] = Field(default=None, max_length=100)
    note: Optional[str] = Field(default=None, max_length=300)
    unit_cost: Optional[float] = Field(default=None, ge=0, le=1e9)
    client_ref: Optional[str] = Field(default=None, min_length=8, max_length=64)


@app.post("/api/stock/movement")
def movement(body: Movement, staff=Depends(need("stock.view"))):
    perm = {"delivery": "stock.receive", "waste": "stock.waste", "adjustment": "stock.adjust"}[body.kind]
    if not allowed(staff["role"], perm):
        raise HTTPException(403, {"delivery": "A manager or stock controller must receive deliveries.",
                                  "adjustment": "Only a manager can adjust stock."}.get(body.kind, "Not allowed."))
    with tx() as c:
        if body.kind == "delivery":
            licence.gate(c, "receive")
        created = logic.record_movement(c, staff["id"], body.ingredient_id, body.kind, body.qty, body.reason,
                                        body.note, body.unit_cost, body.client_ref)
    return {"ok": True, "duplicate": not created}


@app.get("/api/stock/log")
def stock_log(staff=Depends(need("stock.view"))):
    with tx() as c:
        day = logic.open_day_row(c)
        if not day:
            return []
        return [dict(r) for r in c.execute(
            """SELECT m.id, m.type, m.qty, m.reason, m.note, m.created_at, i.name, i.unit, s.name AS staff
               FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
               LEFT JOIN staff s ON s.id = m.staff_id
               WHERE m.day_id = ? AND m.type IN ('delivery','waste','adjustment') ORDER BY m.id DESC""", (day["id"],))]


def _movements(c, ingredient_id=None, type_=None, date_from=None, date_to=None, limit=500):
    where, args = ["1=1"], []
    if ingredient_id:
        where.append("m.ingredient_id = ?"); args.append(ingredient_id)
    if type_:
        where.append("m.type = ?"); args.append(type_)
    if date_from:
        where.append("d.business_date >= ?"); args.append(logic.check_date(date_from, "From"))
    if date_to:
        where.append("d.business_date <= ?"); args.append(logic.check_date(date_to, "To"))
    return [dict(r) for r in c.execute(
        f"""SELECT m.id, d.business_date, m.created_at, m.type, i.name, i.unit, m.qty, m.unit_cost, m.reason, m.note,
                   o.number AS order_number, m.po_line_id, s.name AS staff
            FROM stock_movements m JOIN days d ON d.id = m.day_id JOIN ingredients i ON i.id = m.ingredient_id
            LEFT JOIN orders o ON o.id = m.order_id LEFT JOIN staff s ON s.id = m.staff_id
            WHERE {" AND ".join(where)} ORDER BY m.id DESC LIMIT ?""", args + [min(int(limit), 5000)])]


@app.get("/api/stock/movements")
def movements(ingredient_id: Optional[int] = None, type: Optional[str] = None, date_from: Optional[str] = None,
              date_to: Optional[str] = None, limit: int = 500, staff=Depends(need("stock.receive"))):
    with tx() as c:
        return _movements(c, ingredient_id, type, date_from, date_to, limit)


# ---------------------------------------------------------------- suppliers & purchase orders

class Supplier(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=300)
    active: bool = True


@app.get("/api/suppliers")
def suppliers(staff=Depends(need("purchasing"))):
    with tx() as c:
        return [dict(r) for r in c.execute("SELECT * FROM suppliers ORDER BY active DESC, name")]


@app.post("/api/suppliers")
def add_supplier(body: Supplier, staff=Depends(need("purchasing"))):
    with tx() as c:
        if c.execute("SELECT 1 FROM suppliers WHERE name = ?", (body.name.strip(),)).fetchone():
            raise HTTPException(400, "A supplier with that name already exists.")
        sid = c.execute("INSERT INTO suppliers(name, phone, email, notes, active) VALUES (?,?,?,?,?)",
                        (body.name.strip(), body.phone, body.email, body.notes, int(body.active))).lastrowid
        audit(c, staff["id"], "supplier.create", "supplier", sid, body.model_dump())
    return {"id": sid}


@app.put("/api/suppliers/{sid}")
def edit_supplier(sid: int, body: Supplier, staff=Depends(need("purchasing"))):
    with tx() as c:
        if not c.execute("SELECT 1 FROM suppliers WHERE id = ?", (sid,)).fetchone():
            raise HTTPException(404, "Not found.")
        if c.execute("SELECT 1 FROM suppliers WHERE name = ? AND id <> ?", (body.name.strip(), sid)).fetchone():
            raise HTTPException(400, "A supplier with that name already exists.")
        c.execute("UPDATE suppliers SET name=?, phone=?, email=?, notes=?, active=? WHERE id=?",
                  (body.name.strip(), body.phone, body.email, body.notes, int(body.active), sid))
        audit(c, staff["id"], "supplier.edit", "supplier", sid, body.model_dump())
    return {"ok": True}


class POLine(BaseModel):
    ingredient_id: int
    qty_ordered: float = Field(gt=0, le=1e9)
    unit_cost: float = Field(default=0, ge=0, le=1e9)


class PO(BaseModel):
    supplier_id: int
    lines: list[POLine] = Field(min_length=1, max_length=300)
    expected_date: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=300)


class Receive(BaseModel):
    lines: list[dict] = Field(min_length=1, max_length=300)
    note: Optional[str] = Field(default=None, max_length=300)
    client_ref: Optional[str] = Field(default=None, min_length=8, max_length=64)

    @field_validator("lines")
    @classmethod
    def _lines(cls, v):
        for l in v:
            if "line_id" not in l:
                raise ValueError("each line needs line_id")
        return v


@app.get("/api/purchase-orders")
def list_pos(status: Optional[str] = None, staff=Depends(need("purchasing"))):
    with tx() as c:
        q = "SELECT id FROM purchase_orders" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT 200"
        return [logic.po_view(c, r["id"]) for r in c.execute(q, (status,) if status else ())]


@app.get("/api/purchase-orders/{po_id}")
def get_po(po_id: int, staff=Depends(need("purchasing"))):
    with tx() as c:
        return logic.po_view(c, po_id)


@app.post("/api/purchase-orders")
def create_po(body: PO, staff=Depends(need("purchasing"))):
    with tx() as c:
        pid = logic.save_po(c, staff["id"], body.supplier_id, [l.model_dump() for l in body.lines], body.expected_date, body.notes)
        return logic.po_view(c, pid)


@app.put("/api/purchase-orders/{po_id}")
def edit_po(po_id: int, body: PO, staff=Depends(need("purchasing"))):
    with tx() as c:
        logic.save_po(c, staff["id"], body.supplier_id, [l.model_dump() for l in body.lines], body.expected_date, body.notes, po_id)
        return logic.po_view(c, po_id)


def _po_action(po_id, action, staff):
    with tx() as c:
        logic.set_po_status(c, staff["id"], po_id, action)
        return logic.po_view(c, po_id)


@app.post("/api/purchase-orders/{po_id}/order")
def po_order(po_id: int, staff=Depends(need("purchasing"))):
    return _po_action(po_id, "order", staff)


@app.post("/api/purchase-orders/{po_id}/cancel")
def po_cancel(po_id: int, staff=Depends(need("purchasing"))):
    return _po_action(po_id, "cancel", staff)


@app.post("/api/purchase-orders/{po_id}/receive")
def receive_po(po_id: int, body: Receive, staff=Depends(need("stock.receive"))):
    with tx() as c:
        licence.gate(c, "receive")
        out, dup = logic.receive_po(c, staff["id"], po_id, body.lines, body.note, body.client_ref)
        out["duplicate"] = dup
        return out


# ---------------------------------------------------------------- day open / close

class OpenDay(BaseModel):
    business_date: Optional[str] = None
    opening_counts: Optional[dict[str, Optional[float]]] = None


@app.get("/api/day/opening-preview")
def opening_preview(staff=Depends(need("day.manage"))):
    with tx() as c:
        carry = logic.last_closing(c)
        last = logic.last_closed_day(c)
        return {"last_day": dict(last) if last else None, "rows": [
            {**dict(i), "carry_over": carry.get(i["id"])}
            for i in c.execute("SELECT id, name, unit, count_daily FROM ingredients WHERE active = 1 ORDER BY count_daily DESC, name")]}


@app.post("/api/day/open")
def open_day(body: OpenDay, staff=Depends(need("day.manage"))):
    with tx() as c:
        licence.gate(c, "open_day")
        return {"day_id": logic.open_day(c, staff["id"], body.business_date, body.opening_counts)}


class CloseDay(BaseModel):
    counts: dict[str, dict]
    notes: Optional[str] = Field(default=None, max_length=1000)


@app.post("/api/day/close")
def close_day(body: CloseDay, staff=Depends(need("day.manage"))):
    with tx() as c:
        day_id = logic.close_day(c, staff["id"], body.counts, body.notes)
        return logic.day_report(c, day_id)


@app.get("/api/days")
def days(staff=Depends(need("reports.view"))):
    with tx() as c:
        return [dict(r) for r in c.execute(
            """SELECT d.id, d.business_date, d.covers_to, d.status, d.source,
                      (SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.day_id = d.id AND o.status='paid')
                        - (SELECT COALESCE(SUM(amount),0) FROM refunds r WHERE r.day_id = d.id) AS sales,
                      (SELECT COUNT(*) FROM day_stock ds WHERE ds.day_id = d.id AND ds.variance IS NOT NULL AND ABS(ds.variance) > 1e-9) AS variances
               FROM days d ORDER BY d.business_date DESC LIMIT 400""")]


@app.get("/api/reports/day/{day_id}")
def report(day_id: int, staff=Depends(need("reports.view"))):
    with tx() as c:
        return logic.day_report(c, day_id)


@app.get("/api/reports/period")
def period(date_from: str, date_to: str, staff=Depends(need("reports.view"))):
    with tx() as c:
        return logic.period_report(c, date_from, date_to)


@app.get("/api/reports/catalog")
def report_catalog(staff=Depends(need("reports.view"))):
    return {"reports": [{"key": k, "group": g, "title": t} for k, (g, t, _) in reports.CATALOG.items()],
            "eod_pack": reports.EOD_PACK}


@app.get("/api/reports/run/{key}")
def report_run(key: str, date_from: str, date_to: str, format: Literal["json", "csv"] = "json",
               staff=Depends(need("reports.view"))):
    if key not in reports.CATALOG:
        raise HTTPException(404, "Unknown report.")
    with tx() as c:
        rep = reports.run(c, key, date_from, date_to)
        if format == "csv":
            if not allowed(staff["role"], "data.export"):
                raise HTTPException(403, "You don't have permission to export.")
            audit(c, staff["id"], "export.report", key, None, {"from": date_from, "to": date_to}, staff["ip"])
    if format == "csv":
        return Response(reports.to_csv(rep), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="cfc-{key}-{date_from}_{date_to}.csv"'})
    return rep


class Category(BaseModel):
    major_group: Optional[str] = Field(default=None, max_length=40)
    sort: int = Field(default=0, ge=0, le=9999)


@app.put("/api/categories/{cid}")
def edit_category(cid: int, body: Category, staff=Depends(need("catalog.edit"))):
    with tx() as c:
        old = c.execute("SELECT * FROM categories WHERE id = ?", (cid,)).fetchone()
        if not old:
            raise HTTPException(404, "Not found.")
        mg = (body.major_group or "").strip() or None
        c.execute("UPDATE categories SET major_group = ?, sort = ? WHERE id = ?", (mg, body.sort, cid))
        if (old["major_group"] or None) != mg or old["sort"] != body.sort:
            audit(c, staff["id"], "category.edit", "category", cid,
                  {"name": old["name"], "major_group": [old["major_group"], mg], "sort": [old["sort"], body.sort]})
    return {"ok": True}


@app.get("/api/reports/valuation")
def valuation(staff=Depends(need("reports.view"))):
    with tx() as c:
        rows = [r for r in logic.stock_overview(c) if r["active"]]
        return {"lines": rows, "total": logic.money(sum(r["value"] for r in rows)),
                "uncosted": [r["name"] for r in rows if not r["cost_per_unit"] and r["on_hand"] > 0],
                "negative": [r["name"] for r in rows if r["on_hand"] < 0]}


# ---------------------------------------------------------------- exports (CSV)

EXPORTS = {
    "stock": [("name", "Item"), ("sku", "SKU"), ("barcode", "Barcode"), ("unit", "Base unit"), ("on_hand", "On hand"),
              ("incoming", "Incoming (ordered)"), ("par_level", "Reorder level"), ("critical_level", "Critical level"),
              ("status_label", "Status"), ("cost_per_unit", "Cost per base unit"), ("value", "Value"),
              ("avg_daily_usage", "Avg daily usage"), ("suggested_order", "Suggested order"), ("supplier", "Supplier")],
    "movements": [("business_date", "Business date"), ("created_at", "Time"), ("type", "Type"), ("name", "Item"), ("unit", "Unit"),
                  ("qty", "Qty (signed)"), ("unit_cost", "Unit cost"), ("reason", "Reason"), ("note", "Note"),
                  ("order_number", "Receipt #"), ("staff", "By")],
    "sales": [("name", "Menu item"), ("qty", "Qty"), ("amt", "Amount at list price")],
}


@app.get("/api/export/{kind}.csv")
def export_csv(kind: Literal["stock", "movements", "sales"], date_from: Optional[str] = None, date_to: Optional[str] = None,
               staff=Depends(need("data.export"))):
    with tx() as c:
        if kind == "stock":
            rows = logic.stock_overview(c)
        elif kind == "movements":
            rows = _movements(c, date_from=date_from, date_to=date_to, limit=5000)
        else:
            if not (date_from and date_to):
                raise HTTPException(400, "Choose a date range.")
            rows = logic.period_report(c, date_from, date_to)["items"]
        audit(c, staff["id"], "export.csv", kind, None, {"from": date_from, "to": date_to, "rows": len(rows)}, staff["ip"])
    return Response(importer.to_csv(rows, EXPORTS[kind]), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="cfc-{kind}-{date.today()}.csv"'})


# ---------------------------------------------------------------- setup: ingredients & menu

class Ingredient(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    unit: Literal["g", "ml", "pcs"]
    cost_per_unit: float = Field(default=0, ge=0, le=1e7)
    par_level: float = Field(default=0, ge=0, le=1e9)
    critical_level: float = Field(default=0, ge=0, le=1e9)
    count_daily: bool = True
    active: bool = True
    sku: Optional[str] = Field(default=None, max_length=40)
    barcode: Optional[str] = Field(default=None, max_length=40)
    category: Optional[str] = Field(default=None, max_length=40)
    supplier_id: Optional[int] = None


def _blank(v):
    v = (v or "").strip()
    return v or None


def _check_codes(c, table, sku, barcode, own_id=None):
    for col, v in (("sku", sku), ("barcode", barcode)):
        if v and c.execute(f"SELECT 1 FROM {table} WHERE {col} = ? AND id <> ?", (v, own_id or -1)).fetchone():
            raise HTTPException(400, f"That {col.upper() if col == 'sku' else col} is already used by another item.")
    other = "menu_items" if table == "ingredients" else "ingredients"
    if barcode and c.execute(f"SELECT 1 FROM {other} WHERE barcode = ?", (barcode,)).fetchone():
        raise HTTPException(400, "That barcode is already used.")


@app.get("/api/ingredients")
def ingredients(staff=Depends(need("stock.view"))):
    with tx() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ingredients ORDER BY active DESC, name")]
        if not allowed(staff["role"], "reports.view"):
            for r in rows:
                r.pop("cost_per_unit", None)
        return rows


@app.post("/api/ingredients")
def add_ingredient(body: Ingredient, staff=Depends(need("catalog.edit"))):
    with tx() as c:
        if c.execute("SELECT 1 FROM ingredients WHERE name = ?", (body.name.strip(),)).fetchone():
            raise HTTPException(400, "An ingredient with that name already exists.")
        sku, bc = _blank(body.sku), _blank(body.barcode)
        _check_codes(c, "ingredients", sku, bc)
        iid = c.execute(
            """INSERT INTO ingredients(name, unit, cost_per_unit, par_level, critical_level, count_daily, active, sku, barcode, category, supplier_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (body.name.strip(), body.unit, body.cost_per_unit, body.par_level, body.critical_level, int(body.count_daily),
             int(body.active), sku, bc, _blank(body.category), body.supplier_id)).lastrowid
        day = logic.open_day_row(c)
        if day:
            logic.ensure_stock_row(c, day["id"], iid)
        audit(c, staff["id"], "ingredient.create", "ingredient", iid, body.model_dump())
    return {"id": iid}


@app.put("/api/ingredients/{iid}")
def edit_ingredient(iid: int, body: Ingredient, staff=Depends(need("catalog.edit"))):
    with tx() as c:
        row = c.execute("SELECT * FROM ingredients WHERE id = ?", (iid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found.")
        used = c.execute("SELECT 1 FROM stock_movements WHERE ingredient_id = ? UNION SELECT 1 FROM day_stock WHERE ingredient_id = ? LIMIT 1",
                         (iid, iid)).fetchone()
        if used and row["unit"] != body.unit:
            raise HTTPException(400, "Can't change the unit of an ingredient that already has stock history.")
        if c.execute("SELECT 1 FROM ingredients WHERE name = ? AND id <> ?", (body.name.strip(), iid)).fetchone():
            raise HTTPException(400, "An ingredient with that name already exists.")
        sku, bc = _blank(body.sku), _blank(body.barcode)
        _check_codes(c, "ingredients", sku, bc, iid)
        c.execute("""UPDATE ingredients SET name=?, unit=?, cost_per_unit=?, par_level=?, critical_level=?, count_daily=?, active=?,
                     sku=?, barcode=?, category=?, supplier_id=? WHERE id=?""",
                  (body.name.strip(), body.unit, body.cost_per_unit, body.par_level, body.critical_level, int(body.count_daily),
                   int(body.active), sku, bc, _blank(body.category), body.supplier_id, iid))
        changes = {k: {"old": row[k], "new": v} for k, v in body.model_dump().items()
                   if k in row.keys() and row[k] != (int(v) if isinstance(v, bool) else v)}
        if changes:
            audit(c, staff["id"], "ingredient.cost_change" if "cost_per_unit" in changes else "ingredient.edit",
                  "ingredient", iid, changes)
        day = logic.open_day_row(c)
        if day and body.active:
            logic.ensure_stock_row(c, day["id"], iid)
    return {"ok": True}


class RecipeLine(BaseModel):
    ingredient_id: int
    qty: float = Field(gt=0, le=1e7)


class MenuItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category: Optional[str] = Field(default=None, max_length=40)
    price: float = Field(ge=0, le=1e7)
    active: bool = True
    sku: Optional[str] = Field(default=None, max_length=40)
    barcode: Optional[str] = Field(default=None, max_length=40)
    recipe: list[RecipeLine] = Field(default=[], max_length=50)


@app.get("/api/menu-items")
def menu_items(staff=Depends(need("catalog.edit"))):
    with tx() as c:
        items = [dict(r) for r in c.execute(
            """SELECT m.*, c.name AS category FROM menu_items m
               LEFT JOIN categories c ON c.id = m.category_id ORDER BY m.active DESC, c.name, m.name""")]
        for it in items:
            it["recipe"] = [dict(r) for r in c.execute(
                """SELECT rl.ingredient_id, rl.qty, i.name, i.unit, i.cost_per_unit FROM recipe_lines rl
                   JOIN ingredients i ON i.id = rl.ingredient_id WHERE rl.menu_item_id = ? ORDER BY i.name""", (it["id"],))]
            it["food_cost"] = round(sum(r["qty"] * r["cost_per_unit"] for r in it["recipe"]), 2)
    return items


def _save_item(c, body: MenuItem, staff, iid=None):
    cat_id = None
    if body.category and body.category.strip():
        c.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (body.category.strip(),))
        cat_id = c.execute("SELECT id FROM categories WHERE name = ?", (body.category.strip(),)).fetchone()["id"]
    if c.execute("SELECT 1 FROM menu_items WHERE name = ? AND id <> ?", (body.name.strip(), iid or -1)).fetchone():
        raise HTTPException(400, "A menu item with that name already exists.")
    ids = [rl.ingredient_id for rl in body.recipe]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "An ingredient is listed twice in the recipe.")
    for i in ids:
        if not c.execute("SELECT 1 FROM ingredients WHERE id = ?", (i,)).fetchone():
            raise HTTPException(400, "Unknown ingredient in recipe.")
    sku, bc = _blank(body.sku), _blank(body.barcode)
    _check_codes(c, "menu_items", sku, bc, iid)
    if iid is None:
        iid = c.execute("INSERT INTO menu_items(name, category_id, price, active, sku, barcode) VALUES (?,?,?,?,?,?)",
                        (body.name.strip(), cat_id, body.price, int(body.active), sku, bc)).lastrowid
        audit(c, staff["id"], "menu.create", "menu_item", iid, {"name": body.name, "price": body.price})
    else:
        old = c.execute("SELECT * FROM menu_items WHERE id = ?", (iid,)).fetchone()
        if not old:
            raise HTTPException(404, "Not found.")
        c.execute("UPDATE menu_items SET name=?, category_id=?, price=?, active=?, sku=?, barcode=? WHERE id=?",
                  (body.name.strip(), cat_id, body.price, int(body.active), sku, bc, iid))
        if abs(old["price"] - body.price) > 1e-9:
            audit(c, staff["id"], "menu.price_change", "menu_item", iid, {"name": body.name, "old": old["price"], "new": body.price})
    old_recipe = {r["ingredient_id"]: r["qty"] for r in c.execute("SELECT ingredient_id, qty FROM recipe_lines WHERE menu_item_id=?", (iid,))}
    c.execute("DELETE FROM recipe_lines WHERE menu_item_id = ?", (iid,))
    for rl in body.recipe:
        c.execute("INSERT INTO recipe_lines(menu_item_id, ingredient_id, qty) VALUES (?,?,?)", (iid, rl.ingredient_id, rl.qty))
    new_recipe = {rl.ingredient_id: rl.qty for rl in body.recipe}
    if old_recipe != new_recipe:
        audit(c, staff["id"], "menu.recipe_change", "menu_item", iid, {"old": old_recipe, "new": new_recipe})
    return iid


@app.post("/api/menu-items")
def add_item(body: MenuItem, staff=Depends(need("catalog.edit"))):
    with tx() as c:
        return {"id": _save_item(c, body, staff)}


@app.put("/api/menu-items/{iid}")
def edit_item(iid: int, body: MenuItem, staff=Depends(need("catalog.edit"))):
    with tx() as c:
        _save_item(c, body, staff, iid)
    return {"ok": True}


# ---------------------------------------------------------------- staff & settings

class Staff(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    pin: str = Field(default="", max_length=12)
    role: Literal["cashier", "stock_controller", "manager", "admin"]
    active: bool = True


@app.get("/api/staff")
def staff_list(staff=Depends(need("staff.manage"))):
    with tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, name, role, active, must_change_pin, created_at FROM staff ORDER BY active DESC, name")]


def _check_pin(c, pin, role, own_id=None):
    if not valid_pin_format(pin):
        raise HTTPException(400, "PIN must be 4–6 digits.")
    if role in ("manager", "admin") and weak_pin(pin):
        raise HTTPException(400, "Managers and admins can't use repeated or sequential PINs.")
    if c.execute("SELECT 1 FROM staff WHERE pin_hash = ? AND id <> ?", (hash_pin(pin), own_id or -1)).fetchone():
        raise HTTPException(400, "That PIN can't be used. Choose another.")


@app.post("/api/staff")
def add_staff(body: Staff, staff=Depends(need("staff.manage"))):
    with tx() as c:
        _check_pin(c, body.pin, body.role)
        sid = c.execute("INSERT INTO staff(name, pin_hash, role, active, must_change_pin) VALUES (?,?,?,?,1)",
                        (body.name.strip(), hash_pin(body.pin), body.role, int(body.active))).lastrowid
        audit(c, staff["id"], "staff.create", "staff", sid, {"name": body.name, "role": body.role}, staff["ip"])
    return {"ok": True, "id": sid}


@app.put("/api/staff/{sid}")
def edit_staff(sid: int, body: Staff, staff=Depends(need("staff.manage"))):
    with tx() as c:
        old = c.execute("SELECT * FROM staff WHERE id = ?", (sid,)).fetchone()
        if not old:
            raise HTTPException(404, "Not found.")
        if old["role"] == "admin" and (body.role != "admin" or not body.active):
            admins = c.execute("SELECT COUNT(*) FROM staff WHERE role='admin' AND active=1 AND id <> ?", (sid,)).fetchone()[0]
            if admins == 0:
                raise HTTPException(400, "There must always be at least one active admin.")
        if body.pin:
            _check_pin(c, body.pin, body.role, sid)
            c.execute("UPDATE staff SET pin_hash=?, must_change_pin=1 WHERE id=?", (hash_pin(body.pin), sid))
        c.execute("UPDATE staff SET name=?, role=?, active=? WHERE id=?", (body.name.strip(), body.role, int(body.active), sid))
        if not body.active or body.role != old["role"] or body.pin:
            c.execute("DELETE FROM sessions WHERE staff_id = ?", (sid,))
        audit(c, staff["id"], "staff.edit", "staff", sid,
              {"name": [old["name"], body.name], "role": [old["role"], body.role], "active": [bool(old["active"]), body.active],
               "pin_reset": bool(body.pin)}, staff["ip"])
    return {"ok": True}


class Settings(BaseModel):
    business_name: str = Field(min_length=1, max_length=80)
    business_address: str = Field(default="", max_length=120)
    business_pin: str = Field(default="", max_length=20)
    receipt_footer: str = Field(default="", max_length=200)
    variance_tolerance_pct: float = Field(ge=0, le=20)
    tax_rate_pct: float = Field(default=0, ge=0, le=50)
    tax_inclusive: bool = True
    tax_label: str = Field(default="VAT", max_length=12)
    cashier_max_discount_pct: float = Field(default=0, ge=0, le=100)
    critical_pct_of_reorder: float = Field(default=50, ge=0, le=100)
    reorder_cover_days: float = Field(default=3, ge=0, le=60)
    cost_method: Literal["manual", "last_purchase", "weighted_average"] = "manual"
    require_shift: bool = True
    session_idle_minutes: int = Field(default=720, ge=5, le=1440)
    order_types: str = Field(default="Dine in,Takeaway,Delivery", min_length=1, max_length=200)
    ask_guests: bool = False
    serving_periods: str = Field(default=DEFAULT_SETTINGS["serving_periods"], max_length=300)

    @field_validator("order_types")
    @classmethod
    def _types(cls, v):
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if not parts or any(len(p) > 30 for p in parts) or len(set(parts)) != len(parts):
            raise ValueError("list order types separated by commas, each up to 30 characters, no repeats")
        return ",".join(parts)

    @field_validator("serving_periods")
    @classmethod
    def _periods(cls, v):
        from .reports import parse_periods
        parse_periods(v)  # raises ValueError with a readable message
        return v.strip()


@app.put("/api/settings")
def save_settings(body: Settings, staff=Depends(need("settings.edit"))):
    with tx() as c:
        changes = {}
        for k, v in body.model_dump().items():
            v = ("1" if v else "0") if isinstance(v, bool) else (f"{v:g}" if isinstance(v, float) else str(v))
            old = get_setting(c, k)
            if old != v:
                changes[k] = [old, v]
            c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
        if changes:
            audit(c, staff["id"], "settings.change", "settings", None, changes, staff["ip"])
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 200, action: Optional[str] = None, staff=Depends(need("audit.view"))):
    with tx() as c:
        q = """SELECT a.*, s.name AS staff FROM audit_log a LEFT JOIN staff s ON s.id = a.staff_id"""
        args = []
        if action:
            q += " WHERE a.action LIKE ?"
            args.append(action + "%")
        q += " ORDER BY a.id DESC LIMIT ?"
        return [dict(r) for r in c.execute(q, args + [min(limit, 2000)])]


# ---------------------------------------------------------------- import / export

async def _read_upload(file: UploadFile):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 5 MB.")
    if not data:
        raise HTTPException(400, "The file is empty.")
    return data


class _Preview(Exception):
    def __init__(self, result):
        self.result = result


@app.get("/api/excel")
def excel_export(staff=Depends(need("data.export"))):
    with tx() as c:
        data = importer.export_workbook(c)
        audit(c, staff["id"], "export.excel", "recipes", None, None, staff["ip"])
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="cfc-recipes.xlsx"'})


@app.post("/api/excel")
async def excel_import(file: UploadFile = File(...), preview: bool = Query(False), staff=Depends(need("data.import"))):
    """Ingredients + recipes template (.xlsx) or ingredients .csv. preview=true checks everything and saves nothing."""
    data = await _read_upload(file)
    name = (file.filename or "").lower()
    if not name.endswith(".csv") and workbook.is_stock_movement_workbook(data):
        raise HTTPException(400, "This looks like a daily stock-movement workbook. Use 'Import stock history' instead.")
    try:
        with tx() as c:  # any error rolls back the whole import
            stats = importer.import_ingredients_csv(c, data) if name.endswith(".csv") else importer.import_workbook(c, data)
            if preview:
                raise _Preview(stats)
            day = logic.open_day_row(c)
            if day:
                for i in c.execute("SELECT id FROM ingredients WHERE active = 1").fetchall():
                    logic.ensure_stock_row(c, day["id"], i["id"])
            audit(c, staff["id"], "import.recipes", "import", None,
                  {"file": file.filename, **{k: v for k, v in stats.items() if k != "changes"}, "changes": stats.get("changes")}, staff["ip"])
    except _Preview as p:
        return {"preview": True, **p.result}
    except Exception as e:
        if isinstance(e, (HTTPException, ValueError)):
            raise
        raise ValueError("Couldn't read that file. Is it a valid .xlsx or .csv?") from e
    return {"preview": False, **stats}


@app.post("/api/import/stock-workbook")
async def stock_workbook(file: UploadFile = File(...), mapping: str = Form(""), commit: bool = Query(False),
                         year: Optional[int] = Query(None, ge=2000, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
                         staff=Depends(need("data.import"))):
    """Daily stock-movement workbook. Without commit: returns the analysis (items, unit suggestions,
    discrepancies) and saves nothing. With commit + confirmed units: imports as closed history days."""
    data = await _read_upload(file)
    try:
        parsed = workbook.parse(data, file.filename or "", year, month)
    except Exception as e:
        raise ValueError("Couldn't read that workbook. Is it a valid .xlsx file?") from e
    with tx() as c:
        existing = {r["name"].lower(): dict(r) for r in c.execute("SELECT id, name, unit FROM ingredients")}
        dup = c.execute("SELECT created_at FROM import_batches WHERE kind='stock_history' AND sha256=?", (parsed["sha256"],)).fetchone()
        for it in parsed["items"]:
            it["existing"] = existing.get(it["key"])
        if not commit:
            return {"commit": False, "already_imported": dup["created_at"] if dup else None,
                    "year": parsed["year"], "month": parsed["month"], "items": parsed["items"], "issues": parsed["issues"],
                    "counts": parsed["counts"],
                    "sheets": [{"name": s["name"], "dates": s["dates"], "rows": len(s["rows"])} for s in parsed["sheets"]]}
        try:
            m = json.loads(mapping or "{}")
        except ValueError:
            m = None
        if not isinstance(m, dict) or not all(isinstance(v, dict) for v in m.values()):
            raise HTTPException(400, "Unit mapping is not valid.")
        summary = importer.import_history(c, parsed, m, staff["id"], file.filename)
    return {"commit": True, **summary}


# ---------------------------------------------------------------- licence & devices

class LicenceIn(BaseModel):
    token: str = Field(min_length=10, max_length=4000)


@app.get("/api/licence")
def licence_status(staff=Depends(need("licence.manage"))):
    with tx() as c:
        out = licence.status(c)
        out["devices"] = [dict(r) for r in c.execute("SELECT * FROM terminals ORDER BY active DESC, last_seen DESC")]
        return out


@app.post("/api/licence")
def licence_install(body: LicenceIn, staff=Depends(need("licence.manage"))):
    with tx() as c:
        licence.install(c, body.token, staff["id"])
        return licence.status(c)


@app.post("/api/licence/revocations")
def licence_revocations(body: LicenceIn, staff=Depends(need("licence.manage"))):
    with tx() as c:
        return {"revoked": licence.apply_revocations(c, body.token, staff["id"]), "status": licence.status(c)}


class Device(BaseModel):
    label: Optional[str] = Field(default=None, max_length=40)
    active: bool = True


@app.put("/api/devices/{tid}")
def device(tid: str, body: Device, staff=Depends(need("licence.manage"))):
    with tx() as c:
        if not c.execute("SELECT 1 FROM terminals WHERE id = ?", (tid,)).fetchone():
            raise HTTPException(404, "Not found.")
        c.execute("UPDATE terminals SET label = ?, active = ? WHERE id = ?", (body.label, int(body.active), tid))
        audit(c, staff["id"], "device.edit", "terminal", None, {"id": tid, **body.model_dump()})
    return {"ok": True}


# ---------------------------------------------------------------- UI

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.webmanifest"), media_type="application/manifest+json")


if not os.path.isdir(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.WARNING)
_fh = logging.FileHandler(os.path.join(DATA_DIR, "errors.log"))
_fh.setLevel(logging.ERROR)
log.addHandler(_fh)
