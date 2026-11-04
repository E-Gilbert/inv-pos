"""Open checks ("tabs" / MICROS guest checks): a server opens a check for a table or name, adds items
over time and sends them to the kitchen; the cashier takes payment later.

Rules
* Nothing is sold, and no stock moves, until the check is paid. Payment goes through
  logic.create_order with the check's own lines (never the till's copy), so prices, VAT,
  stock deduction, idempotency and shift rules are exactly the same as a direct sale.
* An item that has NOT been sent to the kitchen can be removed by the server.
  An item that HAS been sent needs a manager (or a manager's PIN) and a reason. It is kept,
  marked removed, and shows in the Voids report and the audit log.
* Cancelling a check with sent items needs a manager and a reason, too.
* The day can't close while checks are open.
"""
from .db import audit, now
from .logic import RuleError, create_order, open_day_row, order_types, parse_days, require_open_day, verify_approver, WEEKDAYS
from datetime import date


def _tab(c, tab_id, must_be_open=True):
    t = c.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if not t:
        raise RuleError("Check not found.")
    if must_be_open and t["status"] != "open":
        raise RuleError(f"This check is already {t['status']}.")
    return t


def view(c, tab_id):
    t = c.execute("""SELECT t.*, s.name AS server FROM tabs t JOIN staff s ON s.id = t.staff_id WHERE t.id = ?""", (tab_id,)).fetchone()
    if not t:
        raise RuleError("Check not found.")
    lines = [dict(r) for r in c.execute(
        """SELECT l.id, l.menu_item_id, m.name, m.price, l.qty, l.note, l.sent_at, l.removed_at, l.removed_reason,
                  a.name AS added_by_name, l.added_at
           FROM tab_lines l JOIN menu_items m ON m.id = l.menu_item_id JOIN staff a ON a.id = l.added_by
           WHERE l.tab_id = ? ORDER BY l.id""", (tab_id,))]
    live = [l for l in lines if not l["removed_at"]]
    out = dict(t)
    out["lines"] = lines
    out["total"] = round(sum(l["price"] * l["qty"] for l in live), 2)
    out["unsent"] = sum(1 for l in live if not l["sent_at"])
    return out


def list_open(c):
    return [view(c, r["id"]) for r in c.execute("SELECT id FROM tabs WHERE status = 'open' ORDER BY opened_at")]


def open_tab(c, staff, label, order_type=None, guests=None, client_ref=None):
    if client_ref:
        prev = c.execute("SELECT id FROM tabs WHERE client_ref = ?", (client_ref,)).fetchone()
        if prev:
            return prev["id"]
    day = require_open_day(c)
    label = (label or "").strip()[:30]
    if not label:
        raise RuleError("Give the check a table number or name.")
    if c.execute("SELECT 1 FROM tabs WHERE status = 'open' AND label = ? COLLATE NOCASE", (label,)).fetchone():
        raise RuleError(f"There is already an open check called '{label}'.")
    types = order_types(c)
    order_type = (order_type or types[0]).strip()
    if order_type not in types:
        raise RuleError("Choose an order type: " + ", ".join(types) + ".")
    if guests is not None and not 0 <= int(guests) <= 500:
        raise RuleError("Guest count must be between 0 and 500.")
    tid = c.execute("""INSERT INTO tabs(day_id, label, order_type, guests, staff_id, status, opened_at, client_ref)
                       VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (day["id"], label, order_type, guests, staff["id"], now(), client_ref)).lastrowid
    audit(c, staff["id"], "tab.open", "tab", tid, {"label": label, "order_type": order_type, "guests": guests})
    return tid


def add_items(c, staff, tab_id, items, send=True, batch_ref=None):
    """Adds lines (and marks them sent to the kitchen unless send=False). Returns the lines added in this call."""
    t = _tab(c, tab_id)
    if batch_ref:
        done = c.execute("SELECT id FROM tab_lines WHERE batch_ref = ? AND tab_id = ?", (batch_ref, tab_id)).fetchall()
        if done:
            return [r["id"] for r in done]
    if not items:
        raise RuleError("Add at least one item.")
    day = open_day_row(c)
    weekday = WEEKDAYS[date.fromisoformat(day["business_date"]).weekday()] if day else None
    ids = []
    for it in items[:200]:
        qty = int(it.get("qty", 0))
        if not 1 <= qty <= 1000:
            raise RuleError("Quantities must be between 1 and 1000.")
        mi = c.execute("SELECT id, name, available_days FROM menu_items WHERE id = ? AND active = 1", (int(it["menu_item_id"]),)).fetchone()
        if not mi:
            raise RuleError("An item is no longer on the menu.")
        days = parse_days(mi["available_days"])
        if weekday and days and weekday not in days:
            raise RuleError(f"{mi['name']} is only sold on {', '.join(days)}.")
        note = (it.get("note") or "").strip()[:120] or None
        ids.append(c.execute(
            """INSERT INTO tab_lines(tab_id, menu_item_id, qty, note, added_by, added_at, batch_ref, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["id"], mi["id"], qty, note, staff["id"], now(), batch_ref, now() if send else None)).lastrowid)
    return ids


def send(c, staff, tab_id):
    _tab(c, tab_id)
    rows = [r["id"] for r in c.execute("SELECT id FROM tab_lines WHERE tab_id = ? AND sent_at IS NULL AND removed_at IS NULL", (tab_id,))]
    c.execute("UPDATE tab_lines SET sent_at = ? WHERE tab_id = ? AND sent_at IS NULL AND removed_at IS NULL", (now(), tab_id))
    return rows


def remove_line(c, staff, line_id, reason=None, approver_pin=None):
    l = c.execute("SELECT * FROM tab_lines WHERE id = ?", (line_id,)).fetchone()
    if not l or l["removed_at"]:
        raise RuleError("That item is not on the check.")
    t = _tab(c, l["tab_id"])
    approved = None
    if l["sent_at"]:
        if not (reason or "").strip():
            raise RuleError("This item was already sent to the kitchen. Give a reason to remove it.")
        if staff["role"] in ("manager", "admin"):
            approved = staff["id"]
        else:
            ap = verify_approver(c, approver_pin)
            if not ap:
                raise RuleError("This item was already sent to the kitchen: a manager must approve removing it.")
            approved = ap["id"]
    c.execute("UPDATE tab_lines SET removed_at = ?, removed_by = ?, removed_reason = ?, approved_by = ? WHERE id = ?",
              (now(), staff["id"], (reason or "").strip() or None, approved, line_id))
    if l["sent_at"]:
        mi = c.execute("SELECT name, price FROM menu_items WHERE id = ?", (l["menu_item_id"],)).fetchone()
        audit(c, staff["id"], "tab.item_void", "tab", t["id"],
              {"label": t["label"], "item": mi["name"], "qty": l["qty"], "value": mi["price"] * l["qty"],
               "reason": reason, "approved_by": approved})


def cancel(c, staff, tab_id, reason, approver_pin=None):
    t = _tab(c, tab_id)
    sent = c.execute("SELECT COUNT(*) FROM tab_lines WHERE tab_id = ? AND sent_at IS NOT NULL AND removed_at IS NULL",
                     (tab_id,)).fetchone()[0]
    approved = staff["id"]
    if sent:
        if not (reason or "").strip():
            raise RuleError("Items were sent to the kitchen: give a reason to cancel the check.")
        if staff["role"] not in ("manager", "admin"):
            ap = verify_approver(c, approver_pin)
            if not ap:
                raise RuleError("Items were sent to the kitchen: a manager must approve cancelling.")
            approved = ap["id"]
        c.execute("""UPDATE tab_lines SET removed_at = ?, removed_by = ?, removed_reason = ?, approved_by = ?
                     WHERE tab_id = ? AND removed_at IS NULL""", (now(), staff["id"], "Check cancelled: " + reason.strip(), approved, tab_id))
    c.execute("UPDATE tabs SET status = 'cancelled', closed_at = ?, cancel_reason = ?, cancelled_by = ? WHERE id = ?",
              (now(), (reason or "").strip() or None, approved, tab_id))
    audit(c, staff["id"], "tab.cancel", "tab", tab_id, {"label": t["label"], "reason": reason, "sent_items": sent, "approved_by": approved})


def pay(c, staff, tab_id, payment_method, amount_tendered=None, payment_ref=None, discount=None, approver_pin=None,
        client_ref=None, terminal_id=None, guests=None):
    """Turns the check into a paid order in the same transaction. Retrying with the same client_ref
    after a network drop returns the same order."""
    t = c.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if not t:
        raise RuleError("Check not found.")
    if t["status"] == "paid" and client_ref:
        o = c.execute("SELECT id, client_ref FROM orders WHERE id = ?", (t["order_id"],)).fetchone()
        if o and o["client_ref"] == client_ref:
            return o["id"], True
    _tab(c, tab_id)
    items = [{"menu_item_id": r["menu_item_id"], "qty": r["qty"], "note": r["note"]} for r in c.execute(
        "SELECT menu_item_id, qty, note FROM tab_lines WHERE tab_id = ? AND removed_at IS NULL ORDER BY id", (tab_id,))]
    if not items:
        raise RuleError("The check has no items. Cancel it instead.")
    if c.execute("SELECT 1 FROM tab_lines WHERE tab_id = ? AND sent_at IS NULL AND removed_at IS NULL", (tab_id,)).fetchone():
        send(c, staff, tab_id)  # paying confirms everything on the check
    oid, dup = create_order(c, staff, items, payment_method, amount_tendered, payment_ref, discount, approver_pin,
                            client_ref, terminal_id, t["order_type"], guests if guests is not None else t["guests"], tab=t)
    c.execute("UPDATE tabs SET status = 'paid', closed_at = ?, order_id = ? WHERE id = ?", (now(), oid, tab_id))
    return oid, dup


def item_voids(c, day_ids):
    """Sent items removed from checks before payment (for the Voids report)."""
    ph = ",".join("?" * len(day_ids)) if day_ids else "NULL"
    return [dict(r) for r in c.execute(
        f"""SELECT d.business_date, t.label, l.removed_at, m.name AS item, l.qty, m.price * l.qty AS value, l.removed_reason AS reason,
                   s.name AS removed_by, a.name AS approved_by, sv.name AS server
            FROM tab_lines l JOIN tabs t ON t.id = l.tab_id JOIN days d ON d.id = t.day_id
            JOIN menu_items m ON m.id = l.menu_item_id LEFT JOIN staff s ON s.id = l.removed_by
            LEFT JOIN staff a ON a.id = l.approved_by JOIN staff sv ON sv.id = t.staff_id
            WHERE t.day_id IN ({ph}) AND l.removed_at IS NOT NULL AND l.sent_at IS NOT NULL ORDER BY l.removed_at""", day_ids)]
