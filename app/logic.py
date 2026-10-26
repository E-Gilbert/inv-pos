"""Business rules: selling, refunds, shifts, stock movements, purchasing and reconciliation.

Core formula, per ingredient, per day:

    expected closing = opening + delivered + adjustments - used by sales (net of voids/refunds) - waste
    variance         = counted - expected

A variance outside tolerance can't be saved without a reason. Tomorrow's opening stock is
today's counted closing stock (the carry-over). Stock that has been ordered but not received
("incoming") is never part of any of these numbers.

Every function here expects to run inside db.tx() (one IMMEDIATE transaction), so a sale,
its receipt number and its stock deductions are written together or not at all.
"""
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .db import audit, get_setting, now, setting_float
from .security import hash_pin


class RuleError(Exception):
    """A business rule was broken. The message is shown to staff as-is."""


class Conflict(RuleError):
    """The same request key was reused for a different request."""


VARIANCE_REASONS = [
    "Discarded / spoiled", "Staff meal", "Portioning / measuring", "Delivery not recorded",
    "Sale not recorded", "Counting error", "Unexplained", "Other",
]
WASTE_REASONS = ["Discarded / spoiled", "Overstayed / expired", "Staff meal", "Burnt / kitchen error",
                 "Complimentary", "Returned by customer", "Other"]
ADJUST_REASONS = ["Found stock", "Delivery quantity corrected", "Transfer in", "Transfer out",
                  "Counting error corrected", "Other"]
REFUND_REASONS = ["Customer complaint", "Wrong item served", "Item unavailable", "Overcharged", "Other"]
PAYMENT_METHODS = ("cash", "mpesa", "card")
MPESA_REF = re.compile(r"^[A-Z0-9]{10}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
D2 = Decimal("0.01")
SOLD_TYPES = ("sale", "void", "refund", "external_sale")


def money(x) -> float:
    return float(Decimal(str(x)).quantize(D2, rounding=ROUND_HALF_UP))


def dec(x) -> Decimal:
    return Decimal(str(x or 0))


def request_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def check_date(s, label="Date"):
    if not s or not DATE_RE.match(s):
        raise RuleError(f"{label} must be YYYY-MM-DD.")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise RuleError(f"{label} is not a real date.")
    return s


def verify_approver(c, pin, perm_roles=("manager", "admin")):
    """A manager types their PIN on someone else's till to approve something."""
    if not pin:
        return None
    row = c.execute("SELECT id, name, role FROM staff WHERE pin_hash = ? AND active = 1",
                    (hash_pin(str(pin)),)).fetchone()
    if not row or row["role"] not in perm_roles:
        raise RuleError("Approval PIN is not a manager's PIN.")
    return row


# ---------------------------------------------------------------- days

def open_day_row(c):
    return c.execute("SELECT * FROM days WHERE status = 'open'").fetchone()


def require_open_day(c):
    day = open_day_row(c)
    if not day:
        raise RuleError("No business day is open. A manager must open the day first.")
    return day


def last_closed_day(c):
    return c.execute("SELECT * FROM days WHERE status = 'closed' ORDER BY business_date DESC LIMIT 1").fetchone()


def last_closing(c):
    """{ingredient_id: counted closing} from the most recent closed day."""
    prev = last_closed_day(c)
    if not prev:
        return {}
    rows = c.execute("SELECT ingredient_id, counted FROM day_stock WHERE day_id = ?", (prev["id"],))
    return {r["ingredient_id"]: r["counted"] for r in rows}


def open_day(c, staff_id, business_date=None, opening_counts=None):
    """Open a day. Opening stock = last counted closing, unless a manager enters a count."""
    if open_day_row(c):
        raise RuleError("A day is already open. Close it before opening a new one.")
    business_date = check_date(business_date or date.today().isoformat(), "Business date")
    if c.execute("SELECT 1 FROM days WHERE business_date = ?", (business_date,)).fetchone():
        raise RuleError(f"{business_date} has already been opened and closed.")
    latest = c.execute("SELECT MAX(COALESCE(covers_to, business_date)) FROM days").fetchone()[0]
    if latest and business_date <= latest:
        raise RuleError(f"Business date must be after the last recorded day ({latest}).")

    carry = last_closing(c)
    counts = {}
    for k, v in (opening_counts or {}).items():
        if v in (None, ""):
            continue
        v = float(v)
        if v < 0 or not math.isfinite(v):
            raise RuleError("Opening counts can't be negative.")
        counts[int(k)] = v
    day_id = c.execute(
        "INSERT INTO days(business_date, status, opened_by, opened_at) VALUES (?, 'open', ?, ?)",
        (business_date, staff_id, now()),
    ).lastrowid
    changed = []
    for ing in c.execute("SELECT id, name FROM ingredients WHERE active = 1").fetchall():
        qty = counts.get(ing["id"], carry.get(ing["id"], 0)) or 0
        if ing["id"] in counts and carry.get(ing["id"]) is not None and abs(counts[ing["id"]] - carry[ing["id"]]) > 1e-9:
            changed.append({"ingredient": ing["name"], "carried": carry[ing["id"]], "opening": counts[ing["id"]]})
        c.execute("INSERT INTO day_stock(day_id, ingredient_id, opening) VALUES (?, ?, ?)", (day_id, ing["id"], float(qty)))
    audit(c, staff_id, "day.open", "day", day_id, {"business_date": business_date, "opening_changed": changed})
    return day_id


def ensure_stock_row(c, day_id, ingredient_id):
    """Ingredients added mid-day start at 0 for that day."""
    c.execute("INSERT OR IGNORE INTO day_stock(day_id, ingredient_id, opening) VALUES (?, ?, 0)", (day_id, ingredient_id))


# ---------------------------------------------------------------- shifts

def open_shift_for(c, staff_id):
    return c.execute("SELECT * FROM shifts WHERE staff_id = ? AND status = 'open'", (staff_id,)).fetchone()


def open_shift(c, staff_id, opening_float):
    day = require_open_day(c)
    opening_float = float(opening_float or 0)
    if opening_float < 0 or not math.isfinite(opening_float):
        raise RuleError("Opening float can't be negative.")
    if open_shift_for(c, staff_id):
        raise RuleError("You already have an open shift.")
    sid = c.execute(
        "INSERT INTO shifts(staff_id, day_id, status, opening_float, opened_at) VALUES (?, ?, 'open', ?, ?)",
        (staff_id, day["id"], money(opening_float), now()),
    ).lastrowid
    audit(c, staff_id, "shift.open", "shift", sid, {"float": opening_float})
    return sid


def shift_summary(c, shift_id):
    s = c.execute("""SELECT sh.*, st.name AS staff_name FROM shifts sh JOIN staff st ON st.id = sh.staff_id
                     WHERE sh.id = ?""", (shift_id,)).fetchone()
    if not s:
        raise RuleError("Shift not found.")
    by = {m: 0.0 for m in PAYMENT_METHODS}
    for r in c.execute("""SELECT payment_method, COALESCE(SUM(total), 0) t FROM orders
                          WHERE shift_id = ? AND status = 'paid' GROUP BY payment_method""", (shift_id,)):
        by[r["payment_method"]] = r["t"]
    ref = {m: 0.0 for m in PAYMENT_METHODS}
    for r in c.execute("SELECT method, COALESCE(SUM(amount), 0) t FROM refunds WHERE shift_id = ? GROUP BY method", (shift_id,)):
        ref[r["method"]] = r["t"]
    orders = c.execute("SELECT COUNT(*) FROM orders WHERE shift_id = ? AND status = 'paid'", (shift_id,)).fetchone()[0]
    expected = money(dec(s["opening_float"]) + dec(by["cash"]) - dec(ref["cash"]))
    out = dict(s)
    out.update({"sales_by_method": {k: money(v) for k, v in by.items()},
                "refunds_by_method": {k: money(v) for k, v in ref.items()},
                "orders": orders, "expected_cash_now": expected})
    return out


def close_shift(c, shift_id, staff, counted_cash, notes=None):
    s = c.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if not s or s["status"] != "open":
        raise RuleError("That shift is not open.")
    if s["staff_id"] != staff["id"] and staff["role"] not in ("manager", "admin"):
        raise RuleError("Only a manager can close another person's shift.")
    counted = float(counted_cash)
    if counted < 0 or not math.isfinite(counted):
        raise RuleError("Counted cash can't be negative.")
    summ = shift_summary(c, shift_id)
    variance = money(dec(counted) - dec(summ["expected_cash_now"]))
    if abs(variance) >= 0.005 and not (notes or "").strip():
        raise RuleError(f"Cash is {'over' if variance > 0 else 'short'} by {abs(variance):,.2f}. Add a note to close the shift.")
    c.execute("""UPDATE shifts SET status='closed', closed_at=?, closed_by=?, expected_cash=?, counted_cash=?,
                 cash_variance=?, notes=? WHERE id=?""",
              (now(), staff["id"], summ["expected_cash_now"], money(counted), variance, (notes or "").strip() or None, shift_id))
    audit(c, staff["id"], "shift.close", "shift", shift_id,
          {"expected": summ["expected_cash_now"], "counted": money(counted), "variance": variance})
    return shift_summary(c, shift_id)


# ---------------------------------------------------------------- selling

def price_order(c, items, discount=None):
    """Prices always come from the database, never the till. Returns lines + totals."""
    if not items:
        raise RuleError("The order is empty.")
    if len(items) > 200:
        raise RuleError("Too many lines in one order.")
    lines, subtotal = [], Decimal(0)
    merged = {}
    for it in items:
        qty = int(it.get("qty", 0))
        if qty <= 0 or qty > 1000:
            raise RuleError("Quantities must be between 1 and 1000.")
        mid = int(it["menu_item_id"])
        merged[mid] = merged.get(mid, 0) + qty
    for mid, qty in merged.items():
        mi = c.execute("SELECT id, name, price FROM menu_items WHERE id = ? AND active = 1", (mid,)).fetchone()
        if not mi:
            raise RuleError("An item in the order is no longer on the menu.")
        lines.append((mi, qty))
        subtotal += dec(mi["price"]) * qty
    subtotal = subtotal.quantize(D2, rounding=ROUND_HALF_UP)

    disc = Decimal(0)
    if discount and dec(discount.get("value")) > 0:
        kind = discount.get("type", "amount")
        if kind == "pct":
            pct = dec(discount["value"])
            if pct > 100:
                raise RuleError("Discount can't be more than 100%.")
            disc = (subtotal * pct / 100).quantize(D2, rounding=ROUND_HALF_UP)
        elif kind == "amount":
            disc = dec(discount["value"]).quantize(D2, rounding=ROUND_HALF_UP)
        else:
            raise RuleError("Unknown discount type.")
        if disc > subtotal:
            raise RuleError("Discount can't be more than the order.")
    net = subtotal - disc

    rate = dec(setting_float(c, "tax_rate_pct"))
    inclusive = get_setting(c, "tax_inclusive") == "1"
    if rate > 0:
        if inclusive:
            tax = (net * rate / (100 + rate)).quantize(D2, rounding=ROUND_HALF_UP)
            total = net
        else:
            tax = (net * rate / 100).quantize(D2, rounding=ROUND_HALF_UP)
            total = net + tax
    else:
        tax, total = Decimal(0), net
    return {"lines": lines, "subtotal": subtotal, "discount": disc, "tax": tax, "total": total,
            "tax_rate": float(rate), "tax_inclusive": inclusive}


def order_types(c):
    return [t.strip() for t in (get_setting(c, "order_types") or "").split(",") if t.strip()] or ["Dine in"]


def create_order(c, staff, items, payment_method, amount_tendered=None, payment_ref=None,
                 discount=None, approver_pin=None, client_ref=None, terminal_id=None,
                 order_type=None, guests=None):
    """Returns (order_id, duplicate). A repeated client_ref returns the original order and
    changes nothing, so a till that retries after a network drop can never double-charge or
    double-deduct stock."""
    payload = {"items": sorted((int(i["menu_item_id"]), int(i.get("qty", 0))) for i in items or []),
               "pm": payment_method, "tend": amount_tendered, "ref": (payment_ref or "").strip().upper(),
               "disc": discount, "ot": order_type, "g": guests}
    rh = request_hash(payload)
    if client_ref:
        prev = c.execute("SELECT id, request_hash, staff_id FROM orders WHERE client_ref = ?", (client_ref,)).fetchone()
        if prev:
            if prev["request_hash"] != rh or prev["staff_id"] != staff["id"]:
                raise Conflict("This checkout key was already used for a different order. Start a new order.")
            return prev["id"], True

    day = require_open_day(c)
    if payment_method not in PAYMENT_METHODS:
        raise RuleError("Choose a payment method.")
    shift = open_shift_for(c, staff["id"])
    if get_setting(c, "require_shift") == "1" and not shift:
        raise RuleError("Open your shift (enter the cash float) before selling.")

    types = order_types(c)
    order_type = (order_type or types[0]).strip()
    if order_type not in types:
        raise RuleError("Choose an order type: " + ", ".join(types) + ".")
    if guests is not None:
        guests = int(guests)
        if not 0 <= guests <= 500:
            raise RuleError("Guest count must be between 0 and 500.")

    p = price_order(c, items, discount)
    discount_by = None
    if p["discount"] > 0:
        reason = ((discount or {}).get("reason") or "").strip()
        if not reason:
            raise RuleError("Give a reason for the discount.")
        pct = float(p["discount"] / p["subtotal"] * 100) if p["subtotal"] else 100
        limit = setting_float(c, "cashier_max_discount_pct")
        if staff["role"] in ("manager", "admin"):
            discount_by = staff["id"]
        elif pct <= limit + 1e-9:
            discount_by = staff["id"]
        else:
            approver = verify_approver(c, approver_pin)
            if not approver:
                raise RuleError(f"Discounts over {limit:g}% need a manager's PIN.")
            discount_by = approver["id"]
    total = p["total"]

    tendered = None
    if payment_method == "cash":
        if amount_tendered is None or dec(amount_tendered) < total:
            raise RuleError(f"Cash received must be at least {get_setting(c, 'currency')} {total:,.2f}.")
        tendered = money(amount_tendered)
        payment_ref = None
    elif payment_method == "mpesa":
        payment_ref = (payment_ref or "").strip().upper()
        if not MPESA_REF.match(payment_ref):
            raise RuleError("Enter the 10-character M-Pesa confirmation code.")
        dup = c.execute("SELECT number, day_id FROM orders WHERE payment_method='mpesa' AND payment_ref = ? AND status = 'paid'",
                        (payment_ref,)).fetchone()
        if dup:
            raise RuleError(f"M-Pesa code {payment_ref} was already used for receipt #{dup['number']}. Check the customer's message.")
    else:
        payment_ref = (payment_ref or "").strip()[:40] or None

    number = c.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM orders WHERE day_id = ?", (day["id"],)).fetchone()[0]
    oid = c.execute(
        """INSERT INTO orders(day_id, number, staff_id, created_at, subtotal, discount, discount_reason, discount_by,
                              tax, tax_rate, tax_inclusive, total, payment_method, amount_tendered, payment_ref,
                              payment_status, shift_id, client_ref, request_hash, terminal_id, order_type, guests)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'manual_confirmed',?,?,?,?,?,?)""",
        (day["id"], number, staff["id"], now(), float(p["subtotal"]), float(p["discount"]),
         ((discount or {}).get("reason") or "").strip() or None, discount_by, float(p["tax"]), p["tax_rate"],
         int(p["tax_inclusive"]), float(total), payment_method, tendered, payment_ref,
         shift["id"] if shift else None, client_ref, rh, terminal_id, order_type, guests),
    ).lastrowid

    for mi, qty in p["lines"]:
        oi = c.execute("INSERT INTO order_items(order_id, menu_item_id, name, price, qty) VALUES (?, ?, ?, ?, ?)",
                       (oid, mi["id"], mi["name"], mi["price"], qty)).lastrowid
        # Deduct raw materials using the recipe as it is right now (snapshot), with today's cost.
        for rl in c.execute("""SELECT rl.ingredient_id, rl.qty, i.cost_per_unit FROM recipe_lines rl
                               JOIN ingredients i ON i.id = rl.ingredient_id WHERE rl.menu_item_id = ?""", (mi["id"],)).fetchall():
            ensure_stock_row(c, day["id"], rl["ingredient_id"])
            c.execute(
                """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, unit_cost, order_id, order_item_id, staff_id, created_at)
                   VALUES (?, ?, 'sale', ?, ?, ?, ?, ?, ?)""",
                (day["id"], rl["ingredient_id"], -rl["qty"] * qty, rl["cost_per_unit"], oid, oi, staff["id"], now()),
            )
    if p["discount"] > 0:
        audit(c, staff["id"], "order.discount", "order", oid,
              {"subtotal": float(p["subtotal"]), "discount": float(p["discount"]), "approved_by": discount_by,
               "reason": (discount or {}).get("reason")})
    return oid, False


def void_order(c, order_id, staff_id, reason):
    day = require_open_day(c)
    if not (reason or "").strip():
        raise RuleError("A reason is required to void an order.")
    order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise RuleError("Order not found.")
    if order["day_id"] != day["id"]:
        raise RuleError("Only orders from the current open day can be voided. Use a refund for older orders.")
    if order["status"] == "void":
        raise RuleError("This order is already void.")
    if order["refunded"] and order["refunded"] > 0:
        raise RuleError("This order has refunds. Refund the remaining items instead of voiding.")
    if order["shift_id"]:
        sh = c.execute("SELECT status FROM shifts WHERE id = ?", (order["shift_id"],)).fetchone()
        if sh and sh["status"] == "closed" and order["payment_method"] == "cash":
            raise RuleError("The cashier's shift is already closed and its cash counted. Use a refund instead.")
    c.execute("UPDATE orders SET status = 'void', void_reason = ?, voided_by = ? WHERE id = ?",
              (reason.strip(), staff_id, order_id))
    for m in c.execute("SELECT * FROM stock_movements WHERE order_id = ? AND type = 'sale'", (order_id,)).fetchall():
        c.execute(
            """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, unit_cost, order_id, order_item_id, reason, staff_id, created_at)
               VALUES (?, ?, 'void', ?, ?, ?, ?, ?, ?, ?)""",
            (day["id"], m["ingredient_id"], -m["qty"], m["unit_cost"], order_id, m["order_item_id"], reason.strip(), staff_id, now()),
        )
    audit(c, staff_id, "order.void", "order", order_id, {"number": order["number"], "total": order["total"], "reason": reason.strip()})


def refundable_lines(c, order_id):
    rows = c.execute("""SELECT oi.id, oi.name, oi.price, oi.qty,
                               COALESCE((SELECT SUM(rl.qty) FROM refund_lines rl WHERE rl.order_item_id = oi.id), 0) AS refunded_qty
                        FROM order_items oi WHERE oi.order_id = ? ORDER BY oi.id""", (order_id,)).fetchall()
    return [dict(r) | {"refundable_qty": r["qty"] - r["refunded_qty"]} for r in rows]


def refund_order(c, staff, order_id, lines, reason, restock=False, method=None, client_ref=None, approver_pin=None):
    if client_ref:
        prev = c.execute("SELECT id FROM refunds WHERE client_ref = ?", (client_ref,)).fetchone()
        if prev:
            return prev["id"], True
    day = require_open_day(c)
    reason = (reason or "").strip()
    if not reason:
        raise RuleError("A reason is required for a refund.")
    approved_by = staff["id"]
    if staff["role"] not in ("manager", "admin"):
        approver = verify_approver(c, approver_pin)
        if not approver:
            raise RuleError("A manager must approve refunds.")
        approved_by = approver["id"]
    order = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise RuleError("Order not found.")
    if order["status"] != "paid":
        raise RuleError("Only paid orders can be refunded.")
    method = method or order["payment_method"]
    if method not in PAYMENT_METHODS:
        raise RuleError("Choose how the money is returned.")

    avail = {l["id"]: l for l in refundable_lines(c, order_id)}
    want = {}
    for l in lines or []:
        oi, q = int(l["order_item_id"]), int(l["qty"])
        if q <= 0:
            continue
        want[oi] = want.get(oi, 0) + q
    if not want:
        raise RuleError("Choose at least one item to refund.")
    subtotal = dec(order["subtotal"] or order["total"])
    amount, tax, rlines = Decimal(0), Decimal(0), []
    for oi, q in want.items():
        line = avail.get(oi)
        if not line:
            raise RuleError("That item is not on this order.")
        if q > line["refundable_qty"]:
            raise RuleError(f"Only {line['refundable_qty']} × {line['name']} can still be refunded.")
        gross = dec(line["price"]) * q
        share = (dec(order["total"]) * gross / subtotal) if subtotal else Decimal(0)
        amount += share
        tax += (dec(order["tax"]) * gross / subtotal) if subtotal else Decimal(0)
        rlines.append((line, q, share))
    fully = all(avail[i]["refundable_qty"] - want.get(i, 0) == 0 for i in avail)
    if fully:  # last refund takes exactly what's left, so rounding never leaves cents behind
        amount = dec(order["total"]) - dec(order["refunded"])
        tax = dec(order["tax"]) - dec(c.execute("SELECT COALESCE(SUM(tax),0) FROM refunds WHERE order_id=?", (order_id,)).fetchone()[0])
    amount = amount.quantize(D2, rounding=ROUND_HALF_UP)
    tax = tax.quantize(D2, rounding=ROUND_HALF_UP)

    if restock and c.execute("SELECT 1 FROM stock_movements WHERE order_id = ? AND type='sale' AND order_item_id IS NULL LIMIT 1",
                             (order_id,)).fetchone():
        raise RuleError("This order was sold before item-level stock tracking; it can be refunded but not restocked.")

    shift = open_shift_for(c, staff["id"])
    rid = c.execute(
        """INSERT INTO refunds(order_id, day_id, shift_id, amount, tax, method, restocked, reason, staff_id, approved_by, client_ref, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, day["id"], shift["id"] if shift else None, float(amount), float(tax), method, int(bool(restock)),
         reason, staff["id"], approved_by, client_ref, now()),
    ).lastrowid
    for line, q, share in rlines:
        c.execute("INSERT INTO refund_lines(refund_id, order_item_id, qty, amount) VALUES (?,?,?,?)",
                  (rid, line["id"], q, money(share)))
        if restock:
            for m in c.execute("SELECT * FROM stock_movements WHERE order_item_id = ? AND type = 'sale'", (line["id"],)).fetchall():
                ensure_stock_row(c, day["id"], m["ingredient_id"])
                c.execute(
                    """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, unit_cost, order_id, order_item_id,
                                                   refund_id, reason, staff_id, created_at)
                       VALUES (?, ?, 'refund', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (day["id"], m["ingredient_id"], -m["qty"] * q / line["qty"], m["unit_cost"], order_id, line["id"],
                     rid, reason, staff["id"], now()),
                )
    c.execute("UPDATE orders SET refunded = ROUND(refunded + ?, 2) WHERE id = ?", (float(amount), order_id))
    audit(c, staff["id"], "order.refund", "order", order_id,
          {"refund_id": rid, "amount": float(amount), "method": method, "restocked": bool(restock),
           "approved_by": approved_by, "reason": reason, "lines": {str(k): v for k, v in want.items()}})
    return rid, False


# ---------------------------------------------------------------- stock in / out

def on_hand(c, ingredient_id):
    day = open_day_row(c)
    if day:
        for l in day_lines(c, day["id"], ingredient_id):
            return l["expected"]
        return 0.0
    return (last_closing(c).get(ingredient_id)) or 0.0


def apply_cost(c, ingredient_id, qty, unit_cost, staff_id):
    """Update an ingredient's cost after receiving goods, according to the costing method."""
    if unit_cost is None or unit_cost <= 0:
        return
    method = get_setting(c, "cost_method")
    ing = c.execute("SELECT cost_per_unit FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    old = ing["cost_per_unit"] or 0
    if method == "last_purchase":
        new = unit_cost
    elif method == "weighted_average":
        have = max(on_hand(c, ingredient_id) - qty, 0)  # stock before this delivery (delivery already recorded)
        new = (have * old + qty * unit_cost) / (have + qty) if (have + qty) > 0 else unit_cost
    else:
        return
    new = round(new, 6)
    if abs(new - old) > 1e-9:
        c.execute("UPDATE ingredients SET cost_per_unit = ? WHERE id = ?", (new, ingredient_id))
        audit(c, staff_id, "ingredient.cost_auto", "ingredient", ingredient_id,
              {"old": old, "new": new, "method": method, "received_qty": qty, "received_unit_cost": unit_cost})


def record_movement(c, staff_id, ingredient_id, kind, qty, reason=None, note=None, unit_cost=None, client_ref=None):
    if client_ref and c.execute("SELECT 1 FROM stock_movements WHERE client_ref = ?", (client_ref,)).fetchone():
        return False
    day = require_open_day(c)
    qty = float(qty)
    if not math.isfinite(qty) or abs(qty) > 1e9:
        raise RuleError("Quantity is not a valid number.")
    if kind not in ("delivery", "waste", "adjustment"):
        raise RuleError("Unknown stock movement.")
    if kind == "adjustment":
        if qty == 0:
            raise RuleError("Adjustment can't be 0.")
        if not (reason or "").strip():
            raise RuleError("Choose a reason for the adjustment.")
        signed = qty
    else:
        if qty <= 0:
            raise RuleError("Quantity must be more than 0.")
        signed = qty if kind == "delivery" else -qty
    if kind == "waste" and not (reason or "").strip():
        raise RuleError("Choose a reason for the waste.")
    if unit_cost is not None and (unit_cost < 0 or not math.isfinite(unit_cost)):
        raise RuleError("Cost can't be negative.")
    ing = c.execute("SELECT id, cost_per_unit, active FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    if not ing:
        raise RuleError("Ingredient not found.")
    ensure_stock_row(c, day["id"], ingredient_id)
    cost = unit_cost if (kind == "delivery" and unit_cost) else ing["cost_per_unit"]
    mid = c.execute(
        """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, unit_cost, reason, note, staff_id, client_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (day["id"], ingredient_id, kind, signed, cost, (reason or "").strip() or None, (note or "").strip() or None,
         staff_id, client_ref, now()),
    ).lastrowid
    if kind == "delivery":
        apply_cost(c, ingredient_id, qty, unit_cost, staff_id)
    if kind == "adjustment":
        audit(c, staff_id, "stock.adjust", "ingredient", ingredient_id, {"qty": signed, "reason": reason, "note": note, "movement": mid})
    return True


# ---------------------------------------------------------------- purchasing

def po_view(c, po_id):
    po = c.execute("""SELECT p.*, s.name AS supplier, st.name AS created_by_name FROM purchase_orders p
                      JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN staff st ON st.id = p.created_by WHERE p.id = ?""",
                   (po_id,)).fetchone()
    if not po:
        raise RuleError("Purchase order not found.")
    lines = [dict(r) for r in c.execute(
        """SELECT l.*, i.name, i.unit FROM po_lines l JOIN ingredients i ON i.id = l.ingredient_id
           WHERE l.po_id = ? ORDER BY i.name""", (po_id,))]
    for l in lines:
        l["outstanding"] = round(max(l["qty_ordered"] - l["qty_received"], 0), 6)
    out = dict(po)
    out["lines"] = lines
    out["value_ordered"] = money(sum(dec(l["qty_ordered"]) * dec(l["unit_cost"]) for l in lines))
    out["value_received"] = money(sum(dec(l["qty_received"]) * dec(l["unit_cost"]) for l in lines))
    return out


def save_po(c, staff_id, supplier_id, lines, expected_date=None, notes=None, po_id=None):
    if not c.execute("SELECT 1 FROM suppliers WHERE id = ? AND active = 1", (supplier_id,)).fetchone():
        raise RuleError("Choose an active supplier.")
    if expected_date:
        check_date(expected_date, "Expected date")
    if not lines:
        raise RuleError("Add at least one item.")
    seen = set()
    for l in lines:
        if l["ingredient_id"] in seen:
            raise RuleError("Each item can only appear once on a purchase order.")
        seen.add(l["ingredient_id"])
        if not (l["qty_ordered"] > 0) or l.get("unit_cost", 0) < 0:
            raise RuleError("Quantities must be more than 0 and costs can't be negative.")
        if not c.execute("SELECT 1 FROM ingredients WHERE id = ?", (l["ingredient_id"],)).fetchone():
            raise RuleError("Unknown item on purchase order.")
    if po_id is None:
        po_id = c.execute("INSERT INTO purchase_orders(supplier_id, status, expected_date, notes, created_by, created_at) VALUES (?, 'draft', ?, ?, ?, ?)",
                          (supplier_id, expected_date, notes, staff_id, now())).lastrowid
        audit(c, staff_id, "po.create", "purchase_order", po_id, {"supplier_id": supplier_id})
    else:
        po = c.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if not po:
            raise RuleError("Purchase order not found.")
        if po["status"] != "draft":
            raise RuleError("Only draft purchase orders can be edited.")
        c.execute("UPDATE purchase_orders SET supplier_id=?, expected_date=?, notes=? WHERE id=?", (supplier_id, expected_date, notes, po_id))
        c.execute("DELETE FROM po_lines WHERE po_id = ?", (po_id,))
        audit(c, staff_id, "po.edit", "purchase_order", po_id, None)
    for l in lines:
        c.execute("INSERT INTO po_lines(po_id, ingredient_id, qty_ordered, unit_cost) VALUES (?,?,?,?)",
                  (po_id, l["ingredient_id"], float(l["qty_ordered"]), float(l.get("unit_cost") or 0)))
    return po_id


def set_po_status(c, staff_id, po_id, action):
    po = c.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        raise RuleError("Purchase order not found.")
    if action == "order":
        if po["status"] != "draft":
            raise RuleError("Only a draft can be marked as ordered.")
        c.execute("UPDATE purchase_orders SET status='ordered', ordered_at=? WHERE id=?", (now(), po_id))
    elif action == "cancel":
        if po["status"] in ("received", "cancelled"):
            raise RuleError("This purchase order is already closed.")
        received = c.execute("SELECT COALESCE(SUM(qty_received),0) FROM po_lines WHERE po_id=?", (po_id,)).fetchone()[0]
        # A part-delivered order is closed short: what arrived stays received, the rest is no longer "incoming".
        c.execute("UPDATE purchase_orders SET status=?, closed_at=? WHERE id=?",
                  ("received" if received > 0 else "cancelled", now(), po_id))
    else:
        raise RuleError("Unknown action.")
    audit(c, staff_id, f"po.{action}", "purchase_order", po_id, {"from": po["status"]})


def receive_po(c, staff_id, po_id, lines, note=None, client_ref=None):
    """Stock goes up ONLY here (or in a direct delivery), when goods physically arrive."""
    if client_ref and c.execute("SELECT 1 FROM stock_movements WHERE client_ref LIKE ?", (client_ref + "#%",)).fetchone():
        return po_view(c, po_id), True
    day = require_open_day(c)
    po = c.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        raise RuleError("Purchase order not found.")
    if po["status"] not in ("ordered", "partial"):
        raise RuleError("Only ordered purchase orders can be received.")
    got = 0
    for l in lines or []:
        qty = float(l.get("qty") or 0)
        if qty == 0:
            continue
        if qty < 0 or not math.isfinite(qty):
            raise RuleError("Received quantity can't be negative.")
        pl = c.execute("SELECT * FROM po_lines WHERE id = ? AND po_id = ?", (int(l["line_id"]), po_id)).fetchone()
        if not pl:
            raise RuleError("That line is not on this purchase order.")
        if pl["qty_received"] + qty > pl["qty_ordered"] + 1e-9:
            raise RuleError("You can't receive more than was ordered. Edit the order or record a separate delivery.")
        cost = l.get("unit_cost")
        cost = pl["unit_cost"] if cost is None else float(cost)
        if cost < 0:
            raise RuleError("Cost can't be negative.")
        ensure_stock_row(c, day["id"], pl["ingredient_id"])
        c.execute(
            """INSERT INTO stock_movements(day_id, ingredient_id, type, qty, unit_cost, po_line_id, note, staff_id, client_ref, created_at)
               VALUES (?, ?, 'delivery', ?, ?, ?, ?, ?, ?, ?)""",
            (day["id"], pl["ingredient_id"], qty, cost, pl["id"], note or f"PO #{po_id}", staff_id,
             f"{client_ref}#{pl['id']}" if client_ref else None, now()),
        )
        c.execute("UPDATE po_lines SET qty_received = qty_received + ?, unit_cost = ? WHERE id = ?", (qty, cost, pl["id"]))
        apply_cost(c, pl["ingredient_id"], qty, cost, staff_id)
        got += 1
    if not got:
        raise RuleError("Enter at least one received quantity.")
    left = c.execute("SELECT COUNT(*) FROM po_lines WHERE po_id = ? AND qty_received < qty_ordered - 1e-9", (po_id,)).fetchone()[0]
    c.execute("UPDATE purchase_orders SET status = ?, closed_at = ? WHERE id = ?",
              ("partial" if left else "received", None if left else now(), po_id))
    audit(c, staff_id, "po.receive", "purchase_order", po_id, {"lines": lines, "note": note})
    return po_view(c, po_id), False


# ---------------------------------------------------------------- reconciliation

def day_lines(c, day_id, ingredient_id=None):
    """Opening, movements and expected closing for every ingredient on a day."""
    rows = c.execute(
        f"""
        SELECT i.id, i.name, i.unit, i.cost_per_unit, i.par_level, i.critical_level, i.count_daily, i.active,
               ds.opening, ds.counted, ds.reason, ds.note,
               COALESCE(SUM(CASE WHEN m.type = 'delivery' THEN m.qty END), 0)     AS delivered,
               COALESCE(SUM(CASE WHEN m.type = 'adjustment' THEN m.qty END), 0)   AS adjusted,
               -COALESCE(SUM(CASE WHEN m.type IN ('sale','void','refund','external_sale') THEN m.qty END), 0) AS sold,
               -COALESCE(SUM(CASE WHEN m.type = 'waste' THEN m.qty END), 0)       AS wasted
        FROM day_stock ds
        JOIN ingredients i ON i.id = ds.ingredient_id
        LEFT JOIN stock_movements m ON m.day_id = ds.day_id AND m.ingredient_id = ds.ingredient_id
        WHERE ds.day_id = ? {"AND ds.ingredient_id = ?" if ingredient_id else ""}
        GROUP BY i.id
        ORDER BY i.count_daily DESC, i.name
        """,
        (day_id, ingredient_id) if ingredient_id else (day_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("opening", "delivered", "adjusted", "sold", "wasted"):
            d[k] = round(d[k] or 0, 3)
        d["expected"] = round(d["opening"] + d["delivered"] + d["adjusted"] - d["sold"] - d["wasted"], 3)
        out.append(d)
    return out


def tolerance_for(line, pct):
    """Allowed difference before a reason is required. Whole items (pcs) must match exactly."""
    if line["unit"] == "pcs":
        return 0.0
    return max(line["opening"] + line["delivered"] + (line.get("adjusted") or 0), 0) * pct / 100.0


def close_day(c, staff_id, counts, notes=None):
    """counts: {ingredient_id: {"counted": number|None, "reason": str, "note": str}}"""
    day = require_open_day(c)
    open_shifts = c.execute("""SELECT st.name FROM shifts sh JOIN staff st ON st.id = sh.staff_id
                               WHERE sh.day_id = ? AND sh.status = 'open'""", (day["id"],)).fetchall()
    if open_shifts:
        raise RuleError("Close these shifts first (count the cash): " + ", ".join(r["name"] for r in open_shifts))
    pct = setting_float(c, "variance_tolerance_pct")
    counts = {int(k): v for k, v in (counts or {}).items()}
    problems, final = [], []

    for line in day_lines(c, day["id"]):
        entry = counts.get(line["id"], {}) or {}
        counted = entry.get("counted")
        counted = None if counted in (None, "") else float(counted)
        reason = (entry.get("reason") or "").strip() or None
        note = (entry.get("note") or "").strip() or None

        if counted is None:
            if line["count_daily"] and line["active"]:
                problems.append(f"{line['name']}: closing count is required.")
                continue
            counted, reason = line["expected"], reason or "Not counted (weekly item)"
        if counted < 0 or not math.isfinite(counted):
            problems.append(f"{line['name']}: count can't be negative.")
            continue

        variance = round(counted - line["expected"], 3)
        if abs(variance) > tolerance_for(line, pct) + 1e-9 and not reason:
            unit, div = {"g": ("kg", 1000), "ml": ("L", 1000)}.get(line["unit"], ("pcs", 1))
            problems.append(f"{line['name']}: {variance / div:+.3g} {unit} difference needs a reason.")
        if reason in ("Other", "Unexplained") and not note and abs(variance) > 0:
            problems.append(f"{line['name']}: add a short note explaining '{reason}'.")
        final.append((line, counted, variance, reason, note))

    if problems:
        raise RuleError("Can't close the day yet:\n" + "\n".join(problems))

    for line, counted, variance, reason, note in final:
        c.execute(
            """UPDATE day_stock SET delivered = ?, adjusted = ?, sold = ?, wasted = ?, expected = ?,
                      counted = ?, variance = ?, reason = ?, note = ?
               WHERE day_id = ? AND ingredient_id = ?""",
            (line["delivered"], line["adjusted"], line["sold"], line["wasted"], line["expected"],
             counted, variance, reason, note, day["id"], line["id"]),
        )
    c.execute("UPDATE days SET status = 'closed', closed_by = ?, closed_at = ?, notes = ? WHERE id = ?",
              (staff_id, now(), notes, day["id"]))
    audit(c, staff_id, "day.close", "day", day["id"],
          {"business_date": day["business_date"], "variances": sum(1 for f in final if abs(f[2]) > 1e-9)})
    return day["id"]


# ---------------------------------------------------------------- stock status

STATUS_META = {
    "out": ("Out of stock", "red"),
    "critical": ("Critical", "red"),
    "low": ("Low stock", "amber"),
    "healthy": ("Healthy", "green"),
    "inactive": ("Inactive", "grey"),
}


def classify(on_hand_qty, par, critical, active, critical_pct):
    """One rule, used everywhere (dashboard, stock list, POS, reports)."""
    if not active:
        return "inactive"
    if on_hand_qty <= 1e-9:
        return "out"
    crit = critical if critical and critical > 0 else (par * critical_pct / 100.0 if par > 0 else 0)
    if crit > 0 and on_hand_qty <= crit + 1e-9:
        return "critical"
    if par > 0 and on_hand_qty <= par + 1e-9:
        return "low"
    return "healthy"


def stock_overview(c):
    """Every ingredient with on-hand, incoming, status and a reorder suggestion."""
    day = open_day_row(c)
    lines = {l["id"]: l for l in day_lines(c, day["id"])} if day else {}
    carry = {} if day else last_closing(c)
    incoming = {r["ingredient_id"]: r["q"] for r in c.execute(
        """SELECT l.ingredient_id, SUM(l.qty_ordered - l.qty_received) q FROM po_lines l
           JOIN purchase_orders p ON p.id = l.po_id WHERE p.status IN ('ordered','partial') GROUP BY l.ingredient_id""")}
    # Physical usage per closed day (opening + delivered + adjusted - counted), last 14 closed days.
    recent = [r["id"] for r in c.execute("SELECT id FROM days WHERE status='closed' ORDER BY business_date DESC LIMIT 14")]
    usage = {}
    if recent:
        q = f"""SELECT ingredient_id, COUNT(*) n,
                       SUM(opening + COALESCE(delivered,0) + COALESCE(adjusted,0) - counted) u
                FROM day_stock WHERE day_id IN ({",".join("?" * len(recent))}) AND counted IS NOT NULL
                GROUP BY ingredient_id"""
        usage = {r["ingredient_id"]: (r["n"], r["u"]) for r in c.execute(q, recent)}
    crit_pct = setting_float(c, "critical_pct_of_reorder")
    cover = setting_float(c, "reorder_cover_days")
    out = []
    for i in c.execute("""SELECT i.*, s.name AS supplier FROM ingredients i LEFT JOIN suppliers s ON s.id = i.supplier_id
                          ORDER BY i.active DESC, i.name"""):
        d = dict(i)
        if day:
            ln = lines.get(i["id"])
            d["on_hand"] = ln["expected"] if ln else 0.0
        else:
            d["on_hand"] = carry.get(i["id"]) or 0.0
        d["incoming"] = round(incoming.get(i["id"], 0) or 0, 3)
        d["status"] = classify(d["on_hand"], i["par_level"], i["critical_level"], i["active"], crit_pct)
        d["status_label"], d["status_colour"] = STATUS_META[d["status"]]
        n, u = usage.get(i["id"], (0, 0))
        d["avg_daily_usage"] = round(u / n, 3) if n >= 3 and u and u > 0 else None
        d["suggested_order"] = None
        if i["active"] and d["status"] in ("out", "critical", "low"):
            if d["avg_daily_usage"] is not None:
                need = d["avg_daily_usage"] * cover + i["par_level"] - d["on_hand"] - d["incoming"]
                d["suggested_order"] = max(round(need, 3), 0)
                if i["unit"] == "pcs":
                    d["suggested_order"] = math.ceil(d["suggested_order"])
        d["value"] = money(max(d["on_hand"], 0) * (i["cost_per_unit"] or 0))
        out.append(d)
    return out


# ---------------------------------------------------------------- reports

def _days_between(c, date_from, date_to):
    return [r["id"] for r in c.execute("SELECT id FROM days WHERE business_date BETWEEN ? AND ? ORDER BY business_date",
                                       (check_date(date_from, "From"), check_date(date_to, "To")))]


def sales_summary(c, day_ids):
    """Revenue, tax, refunds, COGS and gross profit for a set of days.

    gross sales   = list price × qty (before discounts)
    net sales     = what customers paid, less refunds, EXCLUDING tax
    COGS          = ingredient cost of what was sold (recipe × cost at time of sale), net of voids/restocked refunds
    gross profit  = net sales - COGS          (NOT net profit: rent, wages, utilities etc. are not in this system)
    """
    if not day_ids:
        z = {"orders": 0, "gross_sales": 0, "discounts": 0, "tax": 0, "collected": 0, "refunds": 0, "refund_tax": 0,
             "net_sales_ex_tax": 0, "cogs": 0, "gross_profit": 0, "gross_margin_pct": None, "by_method": [],
             "refunds_by_method": [], "items": [], "items_without_recipe": [], "uncosted_ingredients": [], "voids": 0}
        return z
    ph = ",".join("?" * len(day_ids))
    o = c.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(COALESCE(subtotal,total)),0) gross, COALESCE(SUM(discount),0) disc,
                             COALESCE(SUM(tax),0) tax, COALESCE(SUM(total),0) total
                      FROM orders WHERE day_id IN ({ph}) AND status='paid'""", day_ids).fetchone()
    voids = c.execute(f"SELECT COUNT(*) FROM orders WHERE day_id IN ({ph}) AND status='void'", day_ids).fetchone()[0]
    by_method = [dict(r) for r in c.execute(f"""SELECT payment_method, COUNT(*) n, COALESCE(SUM(total),0) amt FROM orders
                                                WHERE day_id IN ({ph}) AND status='paid' GROUP BY payment_method""", day_ids)]
    rf = c.execute(f"SELECT COALESCE(SUM(amount),0) a, COALESCE(SUM(tax),0) t FROM refunds WHERE day_id IN ({ph})", day_ids).fetchone()
    rf_by = [dict(r) for r in c.execute(f"""SELECT method, COUNT(*) n, COALESCE(SUM(amount),0) amt FROM refunds
                                            WHERE day_id IN ({ph}) GROUP BY method""", day_ids)]
    items = [dict(r) for r in c.execute(
        f"""SELECT oi.menu_item_id, oi.name, SUM(oi.qty) qty, SUM(oi.qty * oi.price) amt,
                   (SELECT COUNT(*) FROM recipe_lines rl WHERE rl.menu_item_id = oi.menu_item_id) AS recipe_lines
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            WHERE o.day_id IN ({ph}) AND o.status = 'paid' GROUP BY oi.menu_item_id ORDER BY qty DESC""", day_ids)]
    cogs_rows = c.execute(
        f"""SELECT m.ingredient_id, i.name, SUM(-m.qty * COALESCE(m.unit_cost, i.cost_per_unit)) cost,
                   MAX(CASE WHEN COALESCE(m.unit_cost, i.cost_per_unit) = 0 THEN 1 ELSE 0 END) uncosted
            FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
            WHERE m.day_id IN ({ph}) AND m.type IN ('sale','void','refund') GROUP BY m.ingredient_id""", day_ids).fetchall()
    cogs = sum(dec(r["cost"]) for r in cogs_rows)
    net_ex_tax = dec(o["total"]) - dec(o["tax"]) - (dec(rf["a"]) - dec(rf["t"]))
    gp = net_ex_tax - cogs
    return {
        "orders": o["n"], "voids": voids, "gross_sales": money(o["gross"]), "discounts": money(o["disc"]),
        "tax": money(dec(o["tax"]) - dec(rf["t"])), "collected": money(o["total"]),
        "refunds": money(rf["a"]), "refund_tax": money(rf["t"]),
        "net_sales_ex_tax": money(net_ex_tax), "cogs": money(cogs), "gross_profit": money(gp),
        "gross_margin_pct": round(float(gp / net_ex_tax * 100), 1) if net_ex_tax > 0 else None,
        "by_method": by_method, "refunds_by_method": rf_by, "items": items,
        "items_without_recipe": [r["name"] for r in items if r["recipe_lines"] == 0],
        "uncosted_ingredients": [r["name"] for r in cogs_rows if r["uncosted"]],
    }


def period_report(c, date_from, date_to):
    ids = _days_between(c, date_from, date_to)
    out = sales_summary(c, ids)
    out.update({"from": date_from, "to": date_to, "days": len(ids)})
    ph = ",".join("?" * len(ids)) if ids else "NULL"
    out["purchases"] = [dict(r) for r in c.execute(
        f"""SELECT COALESCE(s.name, 'No supplier / direct') supplier, i.name, i.unit, SUM(m.qty) qty,
                   SUM(m.qty * COALESCE(m.unit_cost, 0)) value, SUM(CASE WHEN COALESCE(m.unit_cost,0)=0 THEN 1 ELSE 0 END) uncosted
            FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
            LEFT JOIN po_lines pl ON pl.id = m.po_line_id LEFT JOIN purchase_orders p ON p.id = pl.po_id
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            WHERE m.day_id IN ({ph}) AND m.type = 'delivery' GROUP BY supplier, i.id ORDER BY supplier, i.name""", ids)] if ids else []
    out["purchases_total"] = money(sum(dec(p["value"]) for p in out["purchases"]))
    out["waste"] = [dict(r) for r in c.execute(
        f"""SELECT i.name, i.unit, SUM(-m.qty) qty, SUM(-m.qty * COALESCE(m.unit_cost, i.cost_per_unit)) value
            FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
            WHERE m.day_id IN ({ph}) AND m.type = 'waste' GROUP BY i.id ORDER BY value DESC""", ids)] if ids else []
    out["waste_value"] = money(sum(dec(w["value"]) for w in out["waste"]))
    out["variance"] = [dict(r) for r in c.execute(
        f"""SELECT i.name, i.unit, SUM(ds.variance) qty, SUM(ds.variance * i.cost_per_unit) value,
                   SUM(CASE WHEN ABS(ds.variance) > 1e-9 THEN 1 ELSE 0 END) days_off
            FROM day_stock ds JOIN ingredients i ON i.id = ds.ingredient_id
            WHERE ds.day_id IN ({ph}) AND ds.variance IS NOT NULL GROUP BY i.id ORDER BY value""", ids)] if ids else []
    out["shortage_value"] = money(-sum(dec(v["value"]) for v in out["variance"] if (v["value"] or 0) < 0))
    return out


def day_report(c, day_id):
    day = c.execute(
        """SELECT d.*, so.name AS opened_by_name, sc.name AS closed_by_name
           FROM days d LEFT JOIN staff so ON so.id = d.opened_by LEFT JOIN staff sc ON sc.id = d.closed_by
           WHERE d.id = ?""", (day_id,)).fetchone()
    if not day:
        raise RuleError("Day not found.")
    pct = setting_float(c, "variance_tolerance_pct")
    s = sales_summary(c, [day_id])
    voids = c.execute(
        """SELECT o.number, o.total, o.void_reason, s.name AS voided_by
           FROM orders o LEFT JOIN staff s ON s.id = o.voided_by
           WHERE o.day_id = ? AND o.status = 'void' ORDER BY o.number""", (day_id,)).fetchall()
    refunds = c.execute(
        """SELECT r.id, o.number, r.amount, r.method, r.restocked, r.reason, s.name AS staff, a.name AS approved_by, r.created_at
           FROM refunds r JOIN orders o ON o.id = r.order_id JOIN staff s ON s.id = r.staff_id
           LEFT JOIN staff a ON a.id = r.approved_by WHERE r.day_id = ? ORDER BY r.id""", (day_id,)).fetchall()
    waste = c.execute(
        """SELECT i.name, i.unit, -m.qty AS qty, m.reason, m.note, s.name AS staff, m.created_at
           FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
           LEFT JOIN staff s ON s.id = m.staff_id
           WHERE m.day_id = ? AND m.type = 'waste' ORDER BY m.created_at""", (day_id,)).fetchall()
    adjustments = c.execute(
        """SELECT i.name, i.unit, m.qty, m.reason, m.note, s.name AS staff, m.created_at
           FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
           LEFT JOIN staff s ON s.id = m.staff_id
           WHERE m.day_id = ? AND m.type = 'adjustment' ORDER BY m.created_at""", (day_id,)).fetchall()
    shifts = [shift_summary(c, r["id"]) for r in c.execute("SELECT id FROM shifts WHERE day_id = ? ORDER BY id", (day_id,))]

    if day["status"] == "closed":
        lines = [dict(r) for r in c.execute(
            """SELECT i.id, i.name, i.unit, i.cost_per_unit, i.par_level, i.count_daily,
                      ds.opening, ds.delivered, COALESCE(ds.adjusted, 0) AS adjusted, ds.sold, ds.wasted, ds.expected,
                      ds.counted, ds.variance, ds.reason, ds.note
               FROM day_stock ds JOIN ingredients i ON i.id = ds.ingredient_id
               WHERE ds.day_id = ? ORDER BY i.count_daily DESC, i.name""", (day_id,))]
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
        l["below_par"] = l["par_level"] > 0 and (l["counted"] if l["counted"] is not None else l["expected"]) <= l["par_level"]

    return {
        "day": dict(day),
        "sales": {"by_method": s["by_method"], "orders": s["orders"], "total": s["collected"],
                  "voids": [dict(r) for r in voids]},
        "summary": s,
        "refunds": [dict(r) for r in refunds],
        "items": s["items"],
        "items_without_recipe": s["items_without_recipe"],
        "waste": [dict(r) for r in waste],
        "adjustments": [dict(r) for r in adjustments],
        "shifts": shifts,
        "stock": lines,
        "tolerance_pct": pct,
        "shortage_value": round(flagged_value, 2),
        "waste_value": round(sum((l["wasted"] or 0) * l["cost_per_unit"] for l in lines), 2),
    }


def recent_dates(days_back=30):
    t = date.today()
    return (t - timedelta(days=days_back)).isoformat(), t.isoformat()


def iso_now():
    return datetime.now().isoformat(timespec="seconds")
