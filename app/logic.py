"""Business rules: selling, stock movements and the end-of-day reconciliation.

Core formula, per ingredient, per day:

    expected closing = opening + delivered - used by sales - recorded waste
    variance         = counted - expected

A variance outside tolerance can't be saved without a reason. Tomorrow's
opening stock is today's counted closing stock (the carry-over).
"""
import re
from datetime import date, datetime

from .db import get_setting


class RuleError(Exception):
    """A business rule was broken. The message is shown to staff as-is."""


VARIANCE_REASONS = [
    "Discarded / spoiled",
    "Staff meal",
    "Portioning / measuring",
    "Delivery not recorded",
    "Sale not recorded",
    "Counting error",
    "Unexplained",
    "Other",
]
WASTE_REASONS = ["Discarded / spoiled", "Staff meal", "Burnt / kitchen error", "Returned by customer", "Other"]
MPESA_REF = re.compile(r"^[A-Z0-9]{10}$")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- days

def open_day_row(c):
    return c.execute("SELECT * FROM days WHERE status = 'open'").fetchone()


def require_open_day(c):
    day = open_day_row(c)
    if not day:
        raise RuleError("No business day is open. A manager must open the day first.")
    return day


def last_closing(c):
    """{ingredient_id: counted closing} from the most recent closed day."""
    prev = c.execute(
        "SELECT id FROM days WHERE status = 'closed' ORDER BY business_date DESC LIMIT 1"
    ).fetchone()
    if not prev:
        return {}
    rows = c.execute("SELECT ingredient_id, counted FROM day_stock WHERE day_id = ?", (prev["id"],))
    return {r["ingredient_id"]: r["counted"] for r in rows}


def open_day(c, staff_id, business_date=None, opening_counts=None):
    """Open a day. Opening stock = yesterday's counted closing, unless a manager
    enters an opening count (needed on the very first day)."""
    if open_day_row(c):
        raise RuleError("A day is already open. Close it before opening a new one.")
    business_date = business_date or date.today().isoformat()
    if c.execute("SELECT 1 FROM days WHERE business_date = ?", (business_date,)).fetchone():
        raise RuleError(f"{business_date} has already been opened and closed.")

    carry = last_closing(c)
    opening_counts = {int(k): v for k, v in (opening_counts or {}).items() if v not in (None, "")}
    cur = c.execute(
        "INSERT INTO days(business_date, status, opened_by, opened_at) VALUES (?, 'open', ?, ?)",
        (business_date, staff_id, now()),
    )
    day_id = cur.lastrowid
    for ing in c.execute("SELECT id FROM ingredients WHERE active = 1").fetchall():
        qty = opening_counts.get(ing["id"], carry.get(ing["id"], 0)) or 0
        if float(qty) < 0:
            raise RuleError("Opening counts can't be negative.")
        c.execute(
            "INSERT INTO day_stock(day_id, ingredient_id, opening) VALUES (?, ?, ?)",
            (day_id, ing["id"], float(qty)),
        )
    return day_id


def ensure_stock_row(c, day_id, ingredient_id):
    """Ingredients added mid-day start at 0 for that day."""
    c.execute(
        "INSERT OR IGNORE INTO day_stock(day_id, ingredient_id, opening) VALUES (?, ?, 0)",
        (day_id, ingredient_id),
    )


# ---------------------------------------------------------------- selling

def create_order(c, staff_id, items, payment_method, amount_tendered=None, payment_ref=None):
    day = require_open_day(c)
    if not items:
        raise RuleError("The order is empty.")
    if payment_method not in ("cash", "mpesa", "card"):
        raise RuleError("Choose a payment method.")

    lines, total = [], 0.0
    for it in items:
        qty = int(it.get("qty", 0))
        if qty <= 0:
            raise RuleError("Quantities must be at least 1.")
        mi = c.execute(
            "SELECT id, name, price FROM menu_items WHERE id = ? AND active = 1", (it["menu_item_id"],)
        ).fetchone()
        if not mi:
            raise RuleError("An item in the order is no longer on the menu.")
        lines.append((mi, qty))
        total += mi["price"] * qty  # price always comes from the database, never the till
    total = round(total, 2)

    if payment_method == "cash":
        if amount_tendered is None or float(amount_tendered) < total:
            raise RuleError(f"Cash received must be at least KES {total:,.0f}.")
    if payment_method == "mpesa":
        payment_ref = (payment_ref or "").strip().upper()
        if not MPESA_REF.match(payment_ref):
            raise RuleError("Enter the 10-character M-Pesa confirmation code.")

    number = c.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 FROM orders WHERE day_id = ?", (day["id"],)
    ).fetchone()[0]
    oid = c.execute(
        """INSERT INTO orders(day_id, number, staff_id, created_at, total, payment_method,
                              amount_tendered, payment_ref)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (day["id"], number, staff_id, now(), total, payment_method,
         float(amount_tendered) if amount_tendered is not None else None, payment_ref or None),
    ).lastrowid

    for mi, qty in lines:
        c.execute(
            "INSERT INTO order_items(order_id, menu_item_id, name, price, qty) VALUES (?, ?, ?, ?, ?)",
            (oid, mi["id"], mi["name"], mi["price"], qty),
        )
        # Deduct raw materials using the recipe as it is right now (snapshot).
        for rl in c.execute(
            "SELECT ingredient_id, qty FROM recipe_lines WHERE menu_item_id = ?", (mi["id"],)
        ).fetchall():
            ensure_stock_row(c, day["id"], rl["ingredient_id"])
            c.execute(
                """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, order_id, staff_id, created_at)
                   VALUES (?, ?, 'sale', ?, ?, ?, ?)""",
                (day["id"], rl["ingredient_id"], -rl["qty"] * qty, oid, staff_id, now()),
            )
    return oid


def void_order(c, order_id, staff_id, reason):
    day = require_open_day(c)
    if not (reason or "").strip():
        raise RuleError("A reason is required to void an order.")
    order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise RuleError("Order not found.")
    if order["day_id"] != day["id"]:
        raise RuleError("Only orders from the current open day can be voided.")
    if order["status"] == "void":
        raise RuleError("This order is already void.")
    c.execute(
        "UPDATE orders SET status = 'void', void_reason = ?, voided_by = ? WHERE id = ?",
        (reason.strip(), staff_id, order_id),
    )
    # Put the raw materials back exactly as they were taken out.
    for m in c.execute(
        "SELECT ingredient_id, qty FROM stock_movements WHERE order_id = ? AND type = 'sale'", (order_id,)
    ).fetchall():
        c.execute(
            """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, order_id, reason, staff_id, created_at)
               VALUES (?, ?, 'void', ?, ?, ?, ?, ?)""",
            (day["id"], m["ingredient_id"], -m["qty"], order_id, reason.strip(), staff_id, now()),
        )


# ---------------------------------------------------------------- stock in / out

def record_movement(c, staff_id, ingredient_id, kind, qty, reason=None, note=None):
    day = require_open_day(c)
    qty = float(qty)
    if qty <= 0:
        raise RuleError("Quantity must be more than 0.")
    if kind not in ("delivery", "waste"):
        raise RuleError("Unknown stock movement.")
    if kind == "waste" and not (reason or "").strip():
        raise RuleError("Choose a reason for the waste.")
    if not c.execute("SELECT 1 FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone():
        raise RuleError("Ingredient not found.")
    ensure_stock_row(c, day["id"], ingredient_id)
    signed = qty if kind == "delivery" else -qty
    c.execute(
        """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, reason, note, staff_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (day["id"], ingredient_id, kind, signed, reason, note, staff_id, now()),
    )


# ---------------------------------------------------------------- reconciliation

def day_lines(c, day_id):
    """Opening, movements and expected closing for every ingredient on a day."""
    rows = c.execute(
        """
        SELECT i.id, i.name, i.unit, i.cost_per_unit, i.par_level, i.count_daily,
               ds.opening, ds.counted, ds.reason, ds.note,
               COALESCE(SUM(CASE WHEN m.type = 'delivery' THEN m.qty END), 0)            AS delivered,
               -COALESCE(SUM(CASE WHEN m.type IN ('sale','void') THEN m.qty END), 0)     AS sold,
               -COALESCE(SUM(CASE WHEN m.type = 'waste' THEN m.qty END), 0)              AS wasted
        FROM day_stock ds
        JOIN ingredients i ON i.id = ds.ingredient_id
        LEFT JOIN stock_movements m ON m.day_id = ds.day_id AND m.ingredient_id = ds.ingredient_id
        WHERE ds.day_id = ?
        GROUP BY i.id
        ORDER BY i.count_daily DESC, i.name
        """,
        (day_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("opening", "delivered", "sold", "wasted"):
            d[k] = round(d[k] or 0, 3)
        d["expected"] = round(d["opening"] + d["delivered"] - d["sold"] - d["wasted"], 3)
        out.append(d)
    return out


def tolerance_for(line, pct):
    """Allowed difference before a reason is required. Whole items (pcs) must match exactly."""
    if line["unit"] == "pcs":
        return 0.0
    return max(line["opening"] + line["delivered"], 0) * pct / 100.0


def close_day(c, staff_id, counts, notes=None):
    """counts: {ingredient_id: {"counted": number|None, "reason": str, "note": str}}"""
    day = require_open_day(c)
    pct = float(get_setting(c, "variance_tolerance_pct"))
    counts = {int(k): v for k, v in (counts or {}).items()}
    problems = []
    final = []

    for line in day_lines(c, day["id"]):
        entry = counts.get(line["id"], {}) or {}
        counted = entry.get("counted")
        counted = None if counted in (None, "") else float(counted)
        reason = (entry.get("reason") or "").strip() or None
        note = (entry.get("note") or "").strip() or None

        if counted is None:
            if line["count_daily"]:
                problems.append(f"{line['name']}: closing count is required.")
                continue
            counted, reason = line["expected"], reason or "Not counted (weekly item)"
        if counted < 0:
            problems.append(f"{line['name']}: count can't be negative.")
            continue

        variance = round(counted - line["expected"], 3)
        if abs(variance) > tolerance_for(line, pct) + 1e-9 and not reason:
            unit, div = {"g": ("kg", 1000), "ml": ("L", 1000)}.get(line["unit"], ("pcs", 1))
            problems.append(
                f"{line['name']}: {variance / div:+.3g} {unit} difference needs a reason."
            )
        if reason in ("Other", "Unexplained") and not note and abs(variance) > 0:
            problems.append(f"{line['name']}: add a short note explaining '{reason}'.")
        final.append((line, counted, variance, reason, note))

    if problems:
        raise RuleError("Can't close the day yet:\n" + "\n".join(problems))

    for line, counted, variance, reason, note in final:
        c.execute(
            """UPDATE day_stock SET delivered = ?, sold = ?, wasted = ?, expected = ?,
                      counted = ?, variance = ?, reason = ?, note = ?
               WHERE day_id = ? AND ingredient_id = ?""",
            (line["delivered"], line["sold"], line["wasted"], line["expected"],
             counted, variance, reason, note, day["id"], line["id"]),
        )
    c.execute(
        "UPDATE days SET status = 'closed', closed_by = ?, closed_at = ?, notes = ? WHERE id = ?",
        (staff_id, now(), notes, day["id"]),
    )
    return day["id"]


# ---------------------------------------------------------------- reports

def day_report(c, day_id):
    day = c.execute(
        """SELECT d.*, so.name AS opened_by_name, sc.name AS closed_by_name
           FROM days d LEFT JOIN staff so ON so.id = d.opened_by LEFT JOIN staff sc ON sc.id = d.closed_by
           WHERE d.id = ?""",
        (day_id,),
    ).fetchone()
    if not day:
        raise RuleError("Day not found.")
    pct = float(get_setting(c, "variance_tolerance_pct"))

    paid = c.execute(
        """SELECT payment_method, COUNT(*) n, COALESCE(SUM(total), 0) amt
           FROM orders WHERE day_id = ? AND status = 'paid' GROUP BY payment_method""",
        (day_id,),
    ).fetchall()
    voids = c.execute(
        """SELECT o.number, o.total, o.void_reason, s.name AS voided_by
           FROM orders o LEFT JOIN staff s ON s.id = o.voided_by
           WHERE o.day_id = ? AND o.status = 'void' ORDER BY o.number""",
        (day_id,),
    ).fetchall()
    items = c.execute(
        """SELECT oi.name, SUM(oi.qty) qty, SUM(oi.qty * oi.price) amt,
                  (SELECT COUNT(*) FROM recipe_lines rl WHERE rl.menu_item_id = oi.menu_item_id) AS recipe_lines
           FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.day_id = ? AND o.status = 'paid'
           GROUP BY oi.menu_item_id ORDER BY qty DESC""",
        (day_id,),
    ).fetchall()
    waste = c.execute(
        """SELECT i.name, i.unit, -m.qty AS qty, m.reason, m.note, s.name AS staff, m.created_at
           FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
           LEFT JOIN staff s ON s.id = m.staff_id
           WHERE m.day_id = ? AND m.type = 'waste' ORDER BY m.created_at""",
        (day_id,),
    ).fetchall()

    if day["status"] == "closed":
        lines = [dict(r) for r in c.execute(
            """SELECT i.id, i.name, i.unit, i.cost_per_unit, i.par_level, i.count_daily,
                      ds.opening, ds.delivered, ds.sold, ds.wasted, ds.expected,
                      ds.counted, ds.variance, ds.reason, ds.note
               FROM day_stock ds JOIN ingredients i ON i.id = ds.ingredient_id
               WHERE ds.day_id = ? ORDER BY i.count_daily DESC, i.name""",
            (day_id,),
        )]
    else:
        lines = day_lines(c, day_id)
        for l in lines:
            l["counted"] = l["variance"] = None

    flagged_value = 0.0
    for l in lines:
        l["tolerance"] = round(tolerance_for(l, pct), 3)
        v = l.get("variance")
        l["variance_value"] = round(v * l["cost_per_unit"], 2) if v is not None else None
        l["flag"] = v is not None and abs(v) > l["tolerance"] + 1e-9
        if l["flag"] and v < 0:
            flagged_value += -l["variance_value"]
        l["below_par"] = l["par_level"] > 0 and (l["counted"] if l["counted"] is not None else l["expected"]) < l["par_level"]

    return {
        "day": dict(day),
        "sales": {
            "by_method": [dict(r) for r in paid],
            "orders": sum(r["n"] for r in paid),
            "total": round(sum(r["amt"] for r in paid), 2),
            "voids": [dict(r) for r in voids],
        },
        "items": [dict(r) for r in items],
        "items_without_recipe": [r["name"] for r in items if r["recipe_lines"] == 0],
        "waste": [dict(r) for r in waste],
        "stock": lines,
        "tolerance_pct": pct,
        "shortage_value": round(flagged_value, 2),
        "waste_value": round(sum(
            (l["wasted"] or 0) * l["cost_per_unit"] for l in lines
        ), 2),
    }
