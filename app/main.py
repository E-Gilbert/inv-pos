"""CFC POS + Smart Inventory — HTTP API and static UI.

Run:  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Then open http://<server-ip>:8000 on any device on the restaurant network.
"""
import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import importer, logic
from .db import BASE_DIR, get_setting, init_db, tx
from .logic import RuleError

app = FastAPI(title="CFC POS + Inventory")
STATIC = os.path.join(BASE_DIR, "static")


@app.on_event("startup")
def startup():
    init_db()
    with tx() as c:
        if not c.execute("SELECT 1 FROM staff").fetchone():
            # First run: one admin so someone can log in and set things up.
            c.execute("INSERT INTO staff(name, pin, role) VALUES ('Admin', '1234', 'admin')")


@app.exception_handler(RuleError)
def rule_error(_: Request, exc: RuleError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------------------------------------------------------- auth

def current_staff(x_token: Optional[str] = Header(default=None)):
    if not x_token:
        raise HTTPException(401, "Please log in.")
    with tx() as c:
        row = c.execute(
            """SELECT s.id, s.name, s.role FROM sessions x JOIN staff s ON s.id = x.staff_id
               WHERE x.token = ? AND s.active = 1""",
            (x_token,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired. Please log in again.")
    return dict(row)


def manager(staff=Depends(current_staff)):
    if staff["role"] not in ("manager", "admin"):
        raise HTTPException(403, "A manager must do this.")
    return staff


def admin(staff=Depends(current_staff)):
    if staff["role"] != "admin":
        raise HTTPException(403, "Only an admin can do this.")
    return staff


class Login(BaseModel):
    pin: str


@app.post("/api/login")
def login(body: Login):
    with tx() as c:
        s = c.execute("SELECT id, name, role FROM staff WHERE pin = ? AND active = 1", (body.pin.strip(),)).fetchone()
        if not s:
            raise HTTPException(401, "Wrong PIN.")
        token = secrets.token_urlsafe(24)
        c.execute("INSERT INTO sessions(token, staff_id) VALUES (?, ?)", (token, s["id"]))
    return {"token": token, "staff": dict(s)}


@app.post("/api/logout")
def logout(x_token: Optional[str] = Header(default=None)):
    with tx() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (x_token,))
    return {"ok": True}


@app.get("/api/state")
def state(staff=Depends(current_staff)):
    with tx() as c:
        day = logic.open_day_row(c)
        return {
            "staff": staff,
            "day": dict(day) if day else None,
            "business_name": get_setting(c, "business_name"),
            "receipt_footer": get_setting(c, "receipt_footer"),
            "tolerance_pct": float(get_setting(c, "variance_tolerance_pct")),
            "variance_reasons": logic.VARIANCE_REASONS,
            "waste_reasons": logic.WASTE_REASONS,
            "first_day": not c.execute("SELECT 1 FROM days WHERE status='closed'").fetchone(),
        }


# ---------------------------------------------------------------- POS

@app.get("/api/menu")
def menu(staff=Depends(current_staff)):
    with tx() as c:
        cats = [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY sort, name")]
        items = [dict(r) for r in c.execute(
            """SELECT m.id, m.name, m.price, m.category_id,
                      (SELECT COUNT(*) FROM recipe_lines rl WHERE rl.menu_item_id = m.id) AS recipe_lines
               FROM menu_items m WHERE m.active = 1 ORDER BY m.name"""
        )]
    return {"categories": cats, "items": items}


class OrderLine(BaseModel):
    menu_item_id: int
    qty: int


class NewOrder(BaseModel):
    items: list[OrderLine]
    payment_method: str
    amount_tendered: Optional[float] = None
    payment_ref: Optional[str] = None


def receipt(c, order_id):
    o = c.execute(
        """SELECT o.*, s.name AS cashier, d.business_date FROM orders o
           JOIN staff s ON s.id = o.staff_id JOIN days d ON d.id = o.day_id WHERE o.id = ?""",
        (order_id,),
    ).fetchone()
    if not o:
        raise HTTPException(404, "Order not found.")
    items = [dict(r) for r in c.execute("SELECT name, price, qty FROM order_items WHERE order_id = ?", (order_id,))]
    out = dict(o)
    out["items"] = items
    out["change"] = round((o["amount_tendered"] or 0) - o["total"], 2) if o["payment_method"] == "cash" else 0
    return out


@app.post("/api/orders")
def new_order(body: NewOrder, staff=Depends(current_staff)):
    with tx() as c:
        oid = logic.create_order(
            c, staff["id"], [i.model_dump() for i in body.items],
            body.payment_method, body.amount_tendered, body.payment_ref,
        )
        return receipt(c, oid)


@app.get("/api/orders")
def list_orders(day_id: Optional[int] = None, staff=Depends(current_staff)):
    with tx() as c:
        if day_id is None:
            day = logic.open_day_row(c)
            if not day:
                return []
            day_id = day["id"]
        return [dict(r) for r in c.execute(
            """SELECT o.id, o.number, o.created_at, o.total, o.payment_method, o.payment_ref, o.status,
                      o.void_reason, s.name AS cashier,
                      (SELECT GROUP_CONCAT(qty || '× ' || name, ', ') FROM order_items WHERE order_id = o.id) AS summary
               FROM orders o JOIN staff s ON s.id = o.staff_id
               WHERE o.day_id = ? ORDER BY o.number DESC""",
            (day_id,),
        )]


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, staff=Depends(current_staff)):
    with tx() as c:
        return receipt(c, order_id)


class Void(BaseModel):
    reason: str


@app.post("/api/orders/{order_id}/void")
def void(order_id: int, body: Void, staff=Depends(manager)):
    with tx() as c:
        logic.void_order(c, order_id, staff["id"], body.reason)
        return receipt(c, order_id)


# ---------------------------------------------------------------- stock

@app.get("/api/stock")
def stock(staff=Depends(current_staff)):
    with tx() as c:
        day = logic.open_day_row(c)
        if not day:
            return {"day": None, "lines": []}
        return {"day": dict(day), "lines": logic.day_lines(c, day["id"])}


class Movement(BaseModel):
    ingredient_id: int
    kind: str  # delivery | waste
    qty: float
    reason: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/stock/movement")
def movement(body: Movement, staff=Depends(current_staff)):
    if body.kind == "delivery" and staff["role"] == "cashier":
        raise HTTPException(403, "A manager must receive deliveries.")
    with tx() as c:
        logic.record_movement(c, staff["id"], body.ingredient_id, body.kind, body.qty, body.reason, body.note)
    return {"ok": True}


@app.get("/api/stock/log")
def stock_log(staff=Depends(current_staff)):
    with tx() as c:
        day = logic.open_day_row(c)
        if not day:
            return []
        return [dict(r) for r in c.execute(
            """SELECT m.id, m.type, m.qty, m.reason, m.note, m.created_at, i.name, i.unit, s.name AS staff
               FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
               LEFT JOIN staff s ON s.id = m.staff_id
               WHERE m.day_id = ? AND m.type IN ('delivery','waste') ORDER BY m.id DESC""",
            (day["id"],),
        )]


# ---------------------------------------------------------------- day open / close

class OpenDay(BaseModel):
    business_date: Optional[str] = None
    opening_counts: Optional[dict[str, Optional[float]]] = None


@app.get("/api/day/opening-preview")
def opening_preview(staff=Depends(manager)):
    with tx() as c:
        carry = logic.last_closing(c)
        return [
            {**dict(i), "carry_over": carry.get(i["id"])}
            for i in c.execute("SELECT id, name, unit, count_daily FROM ingredients WHERE active = 1 ORDER BY count_daily DESC, name")
        ]


@app.post("/api/day/open")
def open_day(body: OpenDay, staff=Depends(manager)):
    with tx() as c:
        return {"day_id": logic.open_day(c, staff["id"], body.business_date, body.opening_counts)}


class CloseDay(BaseModel):
    counts: dict[str, dict]
    notes: Optional[str] = None


@app.post("/api/day/close")
def close_day(body: CloseDay, staff=Depends(manager)):
    with tx() as c:
        day_id = logic.close_day(c, staff["id"], body.counts, body.notes)
        return logic.day_report(c, day_id)


@app.get("/api/days")
def days(staff=Depends(manager)):
    with tx() as c:
        return [dict(r) for r in c.execute(
            """SELECT d.id, d.business_date, d.status,
                      (SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.day_id = d.id AND o.status='paid') AS sales,
                      (SELECT COUNT(*) FROM day_stock ds JOIN ingredients i ON i.id = ds.ingredient_id
                       WHERE ds.day_id = d.id AND ds.variance IS NOT NULL AND ds.variance <> 0) AS variances
               FROM days d ORDER BY d.business_date DESC LIMIT 120"""
        )]


@app.get("/api/reports/day/{day_id}")
def report(day_id: int, staff=Depends(manager)):
    with tx() as c:
        return logic.day_report(c, day_id)


# ---------------------------------------------------------------- setup

class Ingredient(BaseModel):
    name: str
    unit: str
    cost_per_unit: float = 0
    par_level: float = 0
    count_daily: bool = True
    active: bool = True


@app.get("/api/ingredients")
def ingredients(staff=Depends(current_staff)):
    with tx() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ingredients ORDER BY active DESC, name")]


@app.post("/api/ingredients")
def add_ingredient(body: Ingredient, staff=Depends(manager)):
    if body.unit not in ("g", "ml", "pcs"):
        raise HTTPException(400, "Unit must be g, ml or pcs.")
    with tx() as c:
        try:
            iid = c.execute(
                "INSERT INTO ingredients(name, unit, cost_per_unit, par_level, count_daily) VALUES (?,?,?,?,?)",
                (body.name.strip(), body.unit, body.cost_per_unit, body.par_level, int(body.count_daily)),
            ).lastrowid
        except Exception:
            raise HTTPException(400, "An ingredient with that name already exists.")
        day = logic.open_day_row(c)
        if day:
            logic.ensure_stock_row(c, day["id"], iid)
    return {"id": iid}


@app.put("/api/ingredients/{iid}")
def edit_ingredient(iid: int, body: Ingredient, staff=Depends(manager)):
    with tx() as c:
        row = c.execute("SELECT unit FROM ingredients WHERE id = ?", (iid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found.")
        used = c.execute("SELECT 1 FROM stock_movements WHERE ingredient_id = ? LIMIT 1", (iid,)).fetchone()
        if used and row["unit"] != body.unit:
            raise HTTPException(400, "Can't change the unit of an ingredient that already has stock history.")
        c.execute(
            "UPDATE ingredients SET name=?, unit=?, cost_per_unit=?, par_level=?, count_daily=?, active=? WHERE id=?",
            (body.name.strip(), body.unit, body.cost_per_unit, body.par_level, int(body.count_daily), int(body.active), iid),
        )
    return {"ok": True}


class RecipeLine(BaseModel):
    ingredient_id: int
    qty: float


class MenuItem(BaseModel):
    name: str
    category: Optional[str] = None
    price: float
    active: bool = True
    recipe: list[RecipeLine] = []


@app.get("/api/menu-items")
def menu_items(staff=Depends(manager)):
    with tx() as c:
        items = [dict(r) for r in c.execute(
            """SELECT m.*, c.name AS category FROM menu_items m
               LEFT JOIN categories c ON c.id = m.category_id ORDER BY m.active DESC, c.name, m.name"""
        )]
        for it in items:
            it["recipe"] = [dict(r) for r in c.execute(
                """SELECT rl.ingredient_id, rl.qty, i.name, i.unit, i.cost_per_unit FROM recipe_lines rl
                   JOIN ingredients i ON i.id = rl.ingredient_id WHERE rl.menu_item_id = ? ORDER BY i.name""",
                (it["id"],),
            )]
            it["food_cost"] = round(sum(r["qty"] * r["cost_per_unit"] for r in it["recipe"]), 2)
    return items


def _save_item(c, body: MenuItem, iid=None):
    cat_id = None
    if body.category and body.category.strip():
        c.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (body.category.strip(),))
        cat_id = c.execute("SELECT id FROM categories WHERE name = ?", (body.category.strip(),)).fetchone()["id"]
    if body.price < 0:
        raise HTTPException(400, "Price can't be negative.")
    try:
        if iid is None:
            iid = c.execute("INSERT INTO menu_items(name, category_id, price, active) VALUES (?,?,?,?)",
                            (body.name.strip(), cat_id, body.price, int(body.active))).lastrowid
        else:
            c.execute("UPDATE menu_items SET name=?, category_id=?, price=?, active=? WHERE id=?",
                      (body.name.strip(), cat_id, body.price, int(body.active), iid))
    except Exception:
        raise HTTPException(400, "A menu item with that name already exists.")
    c.execute("DELETE FROM recipe_lines WHERE menu_item_id = ?", (iid,))
    for rl in body.recipe:
        if rl.qty <= 0:
            raise HTTPException(400, "Recipe quantities must be more than 0.")
        c.execute(
            """INSERT INTO recipe_lines(menu_item_id, ingredient_id, qty) VALUES (?,?,?)
               ON CONFLICT(menu_item_id, ingredient_id) DO UPDATE SET qty = excluded.qty""",
            (iid, rl.ingredient_id, rl.qty),
        )
    return iid


@app.post("/api/menu-items")
def add_item(body: MenuItem, staff=Depends(manager)):
    with tx() as c:
        return {"id": _save_item(c, body)}


@app.put("/api/menu-items/{iid}")
def edit_item(iid: int, body: MenuItem, staff=Depends(manager)):
    with tx() as c:
        _save_item(c, body, iid)
    return {"ok": True}


class Staff(BaseModel):
    name: str
    pin: str
    role: str
    active: bool = True


@app.get("/api/staff")
def staff_list(staff=Depends(admin)):
    with tx() as c:
        return [dict(r) for r in c.execute("SELECT id, name, role, active FROM staff ORDER BY active DESC, name")]


def _check_staff(body: Staff):
    if body.role not in ("cashier", "manager", "admin"):
        raise HTTPException(400, "Role must be cashier, manager or admin.")
    if not body.pin.isdigit() or not 4 <= len(body.pin) <= 6:
        raise HTTPException(400, "PIN must be 4–6 digits.")


@app.post("/api/staff")
def add_staff(body: Staff, staff=Depends(admin)):
    _check_staff(body)
    with tx() as c:
        try:
            c.execute("INSERT INTO staff(name, pin, role, active) VALUES (?,?,?,?)",
                      (body.name.strip(), body.pin, body.role, int(body.active)))
        except Exception:
            raise HTTPException(400, "That PIN is already used by someone else.")
    return {"ok": True}


@app.put("/api/staff/{sid}")
def edit_staff(sid: int, body: Staff, staff=Depends(admin)):
    if body.pin:
        _check_staff(body)
    with tx() as c:
        try:
            if body.pin:
                c.execute("UPDATE staff SET name=?, pin=?, role=?, active=? WHERE id=?",
                          (body.name.strip(), body.pin, body.role, int(body.active), sid))
            else:
                c.execute("UPDATE staff SET name=?, role=?, active=? WHERE id=?",
                          (body.name.strip(), body.role, int(body.active), sid))
        except Exception:
            raise HTTPException(400, "That PIN is already used by someone else.")
        if not body.active:
            c.execute("DELETE FROM sessions WHERE staff_id = ?", (sid,))
    return {"ok": True}


class Settings(BaseModel):
    business_name: str
    receipt_footer: str
    variance_tolerance_pct: float


@app.put("/api/settings")
def save_settings(body: Settings, staff=Depends(admin)):
    if not 0 <= body.variance_tolerance_pct <= 20:
        raise HTTPException(400, "Tolerance must be between 0 and 20%.")
    with tx() as c:
        for k, v in body.model_dump().items():
            c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (k, str(v)))
    return {"ok": True}


@app.get("/api/excel")
def excel_export(staff=Depends(manager)):
    with tx() as c:
        data = importer.export_workbook(c)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="cfc-recipes.xlsx"'},
    )


@app.post("/api/excel")
async def excel_import(file: UploadFile = File(...), staff=Depends(manager)):
    data = await file.read()
    with tx() as c:  # any error rolls back the whole import
        stats = importer.import_workbook(c, data)
        day = logic.open_day_row(c)
        if day:
            for i in c.execute("SELECT id FROM ingredients WHERE active = 1").fetchall():
                logic.ensure_stock_row(c, day["id"], i["id"])
    return stats


# ---------------------------------------------------------------- UI

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
