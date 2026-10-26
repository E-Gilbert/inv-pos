"""Management reports, modelled on the core MICROS RES 3700 "Autosequences and Reports" set.

Every report takes a business-date range (one day = "daily", several = "consolidated") and
returns the same shape, so the UI, printing and CSV export share one renderer:

    {"key", "title", "from", "to", "days", "sections": [{"title", "columns": [{key, label, type}],
     "rows": [...], "totals": {...} | None, "note": str | None}], "notes": [...]}

Definitions (the same everywhere, and the same as logic.sales_summary):
    gross sales  = menu price × qty, before discounts
    discounts    = order-level discounts (allocated to lines in proportion to their gross)
    net sales    = what customers paid EXCLUDING tax; refunds are subtracted on the day they are given
    collected    = money taken at checkout, including tax (before refunds)
    check        = one paid order (voided orders are excluded from sales and counted separately)
Only paid orders count as sales. Amounts are summed as Decimals and rounded once, so totals add up.
"""
import re
from collections import OrderedDict
from decimal import Decimal

from .db import get_setting
from .logic import _days_between, dec, money, order_types

MONEY, INT, PCT, TEXT, QTY = "money", "int", "pct", "text", "qty"
UNASSIGNED = "Unassigned"


def col(key, label, type_=TEXT):
    return {"key": key, "label": label, "type": type_}


def _ph(ids):
    return ",".join("?" * len(ids)) if ids else "NULL"


def _pct(part, whole):
    return round(float(Decimal(part) / Decimal(whole) * 100), 1) if whole else None


def _finish(rows, money_keys):
    for r in rows:
        for k in money_keys:
            if k in r and r[k] is not None:
                r[k] = money(r[k])
    return rows


# ---------------------------------------------------------------- serving periods

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_periods(text):
    """'Breakfast=05:00-11:00;Lunch=11:00-16:00;Dinner=16:00-05:00' -> [(name, start_min, end_min)].
    A period may run past midnight. Periods may not overlap. Empty text = no periods."""
    out = []
    for part in [p.strip() for p in (text or "").split(";") if p.strip()]:
        if "=" not in part or "-" not in part:
            raise ValueError(f"serving period '{part}' must look like Lunch=11:00-16:00")
        name, rng = [x.strip() for x in part.split("=", 1)]
        a, b = [x.strip() for x in rng.split("-", 1)]
        ma, mb = TIME_RE.match(a), TIME_RE.match(b)
        if not name or not ma or not mb or a == b:
            raise ValueError(f"serving period '{part}' needs a name and two different HH:MM times")
        out.append((name[:30], int(ma[1]) * 60 + int(ma[2]), int(mb[1]) * 60 + int(mb[2])))
    seen = {}
    for name, s, e in out:
        m = s
        while m != e:
            if m in seen:
                raise ValueError(f"serving periods '{seen[m]}' and '{name}' overlap")
            seen[m] = name
            m = (m + 1) % 1440
    return out


def period_of(hhmm, periods):
    m = int(hhmm[:2]) * 60 + int(hhmm[3:5])
    for name, s, e in periods:
        if (s < e and s <= m < e) or (s > e and (m >= s or m < e)):
            return name
    return "Outside serving periods"


# ---------------------------------------------------------------- shared data

def _orders(c, ids, status="paid"):
    return c.execute(
        f"""SELECT o.*, d.business_date, s.name AS cashier FROM orders o
            JOIN days d ON d.id = o.day_id JOIN staff s ON s.id = o.staff_id
            WHERE o.day_id IN ({_ph(ids)}) AND o.status = ? ORDER BY d.business_date, o.number""",
        ids + [status]).fetchall()


def _refunds(c, ids):
    return c.execute(
        f"""SELECT r.*, d.business_date, o.number, o.payment_method AS orig_method, o.order_type,
                   s.name AS staff, a.name AS approver, od.business_date AS order_date
            FROM refunds r JOIN days d ON d.id = r.day_id JOIN orders o ON o.id = r.order_id
            JOIN days od ON od.id = o.day_id JOIN staff s ON s.id = r.staff_id
            LEFT JOIN staff a ON a.id = r.approved_by
            WHERE r.day_id IN ({_ph(ids)}) ORDER BY r.id""", ids).fetchall()


def _ex_tax(o):
    return dec(o["total"]) - dec(o["tax"])


def _refund_ex_tax(r):
    return dec(r["amount"]) - dec(r["tax"])


def _lines(c, ids):
    """Paid order lines with discount share, net sales ex tax, returns and food cost per line."""
    rows = c.execute(
        f"""SELECT oi.id, oi.menu_item_id, oi.name, oi.price, oi.qty, o.subtotal, o.total, o.tax, o.discount,
                   COALESCE(cat.name, '{UNASSIGNED}') AS family, COALESCE(NULLIF(cat.major_group, ''), '{UNASSIGNED}') AS major
            FROM order_items oi JOIN orders o ON o.id = oi.order_id
            LEFT JOIN menu_items m ON m.id = oi.menu_item_id LEFT JOIN categories cat ON cat.id = m.category_id
            WHERE o.day_id IN ({_ph(ids)}) AND o.status = 'paid'""", ids).fetchall()
    cost = {r["order_item_id"]: dec(r["c"]) for r in c.execute(
        f"""SELECT m.order_item_id, SUM(-m.qty * COALESCE(m.unit_cost, i.cost_per_unit)) c
            FROM stock_movements m JOIN ingredients i ON i.id = m.ingredient_id
            JOIN orders o ON o.id = m.order_id
            WHERE o.day_id IN ({_ph(ids)}) AND o.status = 'paid' AND m.type IN ('sale','refund')
              AND m.order_item_id IS NOT NULL
            GROUP BY m.order_item_id""", ids)}
    # Returns are reported on the day the refund was GIVEN (like MICROS), for any original sale date.
    returns = c.execute(
        f"""SELECT rl.qty, rl.amount, r.amount AS r_amount, r.tax AS r_tax, oi.menu_item_id, oi.name,
                   COALESCE(cat.name, '{UNASSIGNED}') AS family, COALESCE(NULLIF(cat.major_group, ''), '{UNASSIGNED}') AS major
            FROM refund_lines rl JOIN refunds r ON r.id = rl.refund_id JOIN order_items oi ON oi.id = rl.order_item_id
            LEFT JOIN menu_items m ON m.id = oi.menu_item_id LEFT JOIN categories cat ON cat.id = m.category_id
            WHERE r.day_id IN ({_ph(ids)})""", ids).fetchall()
    out = []
    for r in rows:
        gross = dec(r["price"]) * r["qty"]
        sub = dec(r["subtotal"] or r["total"])
        share = gross / sub if sub else Decimal(0)
        out.append({"menu_item_id": r["menu_item_id"], "name": r["name"], "family": r["family"], "major": r["major"],
                    "qty": r["qty"], "gross": gross, "discount": dec(r["discount"]) * share,
                    "net": (dec(r["total"]) - dec(r["tax"])) * share, "cost": cost.get(r["id"], Decimal(0))})
    ret = []
    for r in returns:
        ratio = (dec(r["r_amount"]) - dec(r["r_tax"])) / dec(r["r_amount"]) if r["r_amount"] else Decimal(1)
        ret.append({"menu_item_id": r["menu_item_id"], "name": r["name"], "family": r["family"], "major": r["major"],
                    "qty": r["qty"], "net": dec(r["amount"]) * ratio})
    return out, ret


def _group_sales(lines, returns, key, label):
    agg = OrderedDict()
    for l in sorted(lines, key=lambda x: (x[key])):
        a = agg.setdefault(l[key], {"group": l[key], "qty": 0, "gross": Decimal(0), "discount": Decimal(0), "net": Decimal(0),
                                    "ret_qty": 0, "returns": Decimal(0), "cost": Decimal(0)})
        a["qty"] += l["qty"]
        a["gross"] += l["gross"]
        a["discount"] += l["discount"]
        a["net"] += l["net"]
        a["cost"] += l["cost"]
    for r in returns:
        a = agg.setdefault(r[key], {"group": r[key], "qty": 0, "gross": Decimal(0), "discount": Decimal(0), "net": Decimal(0),
                                    "ret_qty": 0, "returns": Decimal(0), "cost": Decimal(0)})
        a["ret_qty"] += r["qty"]
        a["returns"] += r["net"]
    total_net = sum((a["net"] - a["returns"] for a in agg.values()), Decimal(0))
    rows = []
    for a in agg.values():
        net_after = a["net"] - a["returns"]
        rows.append({"group": a["group"], "qty": a["qty"], "ret_qty": a["ret_qty"], "gross": a["gross"],
                     "discount": a["discount"], "returns": a["returns"], "net": net_after,
                     "pct": _pct(net_after, total_net)})
    rows.sort(key=lambda r: -r["net"])
    totals = {"group": "Total", "qty": sum(r["qty"] for r in rows), "ret_qty": sum(r["ret_qty"] for r in rows),
              "gross": sum((r["gross"] for r in rows), Decimal(0)), "discount": sum((r["discount"] for r in rows), Decimal(0)),
              "returns": sum((r["returns"] for r in rows), Decimal(0)), "net": total_net, "pct": 100.0 if rows else None}
    cols = [col("group", label), col("qty", "Qty sold", INT), col("ret_qty", "Qty returned", INT), col("gross", "Gross sales", MONEY),
            col("discount", "Discounts", MONEY), col("returns", "Returns", MONEY), col("net", "Net sales", MONEY), col("pct", "% of net", PCT)]
    keys = ["gross", "discount", "returns", "net"]
    return {"title": label + " sales", "columns": cols, "rows": _finish(rows, keys), "totals": _finish([totals], keys)[0],
            "note": "Net sales exclude tax and are after discounts and returns."}


# ---------------------------------------------------------------- reports

def rpt_sales(c, ids):
    orders = _orders(c, ids)
    voids = _orders(c, ids, "void")
    refunds = _refunds(c, ids)
    checks = len(orders)
    guests = sum(o["guests"] or 0 for o in orders)
    with_guests = sum(1 for o in orders if o["guests"])
    gross = sum((dec(o["subtotal"] or o["total"]) for o in orders), Decimal(0))
    disc = sum((dec(o["discount"]) for o in orders), Decimal(0))
    tax = sum((dec(o["tax"]) for o in orders), Decimal(0))
    collected = sum((dec(o["total"]) for o in orders), Decimal(0))
    ref_amt = sum((dec(r["amount"]) for r in refunds), Decimal(0))
    ref_tax = sum((dec(r["tax"]) for r in refunds), Decimal(0))
    net = collected - tax - (ref_amt - ref_tax)
    void_amt = sum((dec(o["total"]) for o in voids), Decimal(0))
    summary = [
        ("Gross sales (menu prices)", gross, MONEY), ("− Discounts", disc, MONEY),
        ("− Tax in sales", tax, MONEY), ("− Returns / refunds (ex tax)", ref_amt - ref_tax, MONEY),
        ("Net sales (ex tax)", net, MONEY), ("Collected at checkout (incl. tax)", collected, MONEY),
        ("Refunds paid out (incl. tax)", ref_amt, MONEY), ("Net cash-in (collected − refunds)", collected - ref_amt, MONEY),
        ("Checks (paid orders)", checks, INT), ("Average check (collected)", collected / checks if checks else None, MONEY),
        ("Guests (where recorded)", guests, INT),
        ("Average per guest (net)", (sum((_ex_tax(o) for o in orders if o["guests"]), Decimal(0)) / guests) if guests else None, MONEY),
        ("Voided checks", len(voids), INT), ("Voided amount", void_amt, MONEY), ("Refunds given", len(refunds), INT),
    ]
    s_rows = [{"item": a, "value": money(v) if t == MONEY and v is not None else v, "_type": t} for a, v, t in summary]

    tender = OrderedDict((m, {"method": m, "checks": 0, "collected": Decimal(0), "refunds": Decimal(0)}) for m in ("cash", "mpesa", "card"))
    for o in orders:
        tender[o["payment_method"]]["checks"] += 1
        tender[o["payment_method"]]["collected"] += dec(o["total"])
    for r in refunds:
        tender[r["method"]]["refunds"] += dec(r["amount"])
    t_rows = [dict(t, net=t["collected"] - t["refunds"], method={"cash": "Cash", "mpesa": "M-Pesa", "card": "Card"}[t["method"]])
              for t in tender.values()]
    t_tot = {"method": "Total", "checks": checks, "collected": collected, "refunds": ref_amt, "net": collected - ref_amt}

    ot = OrderedDict((t, {"type": t, "checks": 0, "guests": 0, "net": Decimal(0), "collected": Decimal(0)}) for t in order_types(c))
    for o in orders:
        a = ot.setdefault(o["order_type"], {"type": o["order_type"], "checks": 0, "guests": 0, "net": Decimal(0), "collected": Decimal(0)})
        a["checks"] += 1
        a["guests"] += o["guests"] or 0
        a["net"] += _ex_tax(o)
        a["collected"] += dec(o["total"])
    tot_net_before = collected - tax
    o_rows = [dict(a, pct=_pct(a["net"], tot_net_before), avg=a["collected"] / a["checks"] if a["checks"] else None)
              for a in ot.values() if a["checks"] or a["type"] in order_types(c)]

    per_day = OrderedDict()
    for bd in [r["business_date"] for r in c.execute(f"SELECT business_date FROM days WHERE id IN ({_ph(ids)}) ORDER BY business_date", ids)]:
        per_day[bd] = {"date": bd, "checks": 0, "guests": 0, "gross": Decimal(0), "discount": Decimal(0), "tax": Decimal(0),
                       "collected": Decimal(0), "refunds": Decimal(0), "ref_tax": Decimal(0)}
    for o in orders:
        d = per_day[o["business_date"]]
        d["checks"] += 1
        d["guests"] += o["guests"] or 0
        d["gross"] += dec(o["subtotal"] or o["total"])
        d["discount"] += dec(o["discount"])
        d["tax"] += dec(o["tax"])
        d["collected"] += dec(o["total"])
    for r in refunds:
        per_day[r["business_date"]]["refunds"] += dec(r["amount"])
        per_day[r["business_date"]]["ref_tax"] += dec(r["tax"])
    d_rows = []
    for d in per_day.values():
        d_rows.append({"date": d["date"], "checks": d["checks"], "guests": d["guests"], "gross": d["gross"], "discount": d["discount"],
                       "tax": d["tax"] - d["ref_tax"], "refunds": d["refunds"], "net": d["collected"] - d["tax"] - (d["refunds"] - d["ref_tax"]),
                       "collected": d["collected"], "avg": d["collected"] / d["checks"] if d["checks"] else None})
    d_tot = {"date": "Total", "checks": checks, "guests": guests, "gross": gross, "discount": disc, "tax": tax - ref_tax,
             "refunds": ref_amt, "net": net, "collected": collected, "avg": collected / checks if checks else None}
    mk = ["gross", "discount", "tax", "refunds", "net", "collected", "avg"]
    notes = []
    if checks and with_guests < checks:
        notes.append(f"Guest count recorded on {with_guests} of {checks} checks; average per guest uses those checks only.")
    return [
        {"title": "Sales summary", "columns": [col("item", "Line"), col("value", "Amount", "auto")], "rows": s_rows, "totals": None},
        {"title": "Tender media (payments)", "columns": [col("method", "Payment"), col("checks", "Checks", INT), col("collected", "Collected", MONEY),
                                                          col("refunds", "Refunded", MONEY), col("net", "Net", MONEY)],
         "rows": _finish(t_rows, ["collected", "refunds", "net"]), "totals": _finish([t_tot], ["collected", "refunds", "net"])[0],
         "note": "Payments are confirmed by the cashier (cash count, M-Pesa message, card slip); they are not verified with Safaricom or the bank."},
        {"title": "Order types", "columns": [col("type", "Order type"), col("checks", "Checks", INT), col("guests", "Guests", INT),
                                             col("net", "Net sales", MONEY), col("pct", "% of net", PCT), col("avg", "Avg check", MONEY)],
         "rows": _finish(o_rows, ["net", "avg"]), "totals": None, "note": "Before refunds."},
        {"title": "By business day", "columns": [col("date", "Date"), col("checks", "Checks", INT), col("guests", "Guests", INT),
                                                 col("gross", "Gross", MONEY), col("discount", "Discounts", MONEY), col("tax", "Tax", MONEY),
                                                 col("refunds", "Refunds", MONEY), col("net", "Net sales", MONEY),
                                                 col("collected", "Collected", MONEY), col("avg", "Avg check", MONEY)],
         "rows": _finish(d_rows, mk), "totals": _finish([d_tot], mk)[0]},
    ], notes


def rpt_tax(c, ids):
    orders = _orders(c, ids)
    refunds = _refunds(c, ids)
    by = OrderedDict()
    for o in orders:
        k = (o["tax_rate"] or 0, "inclusive" if o["tax_inclusive"] else "exclusive")
        a = by.setdefault(k, {"rate": k[0], "mode": k[1], "checks": 0, "taxable": Decimal(0), "tax": Decimal(0)})
        a["checks"] += 1
        a["taxable"] += _ex_tax(o)
        a["tax"] += dec(o["tax"])
    rows = _finish([dict(a, gross=a["taxable"] + a["tax"]) for a in by.values()], ["taxable", "tax", "gross"])
    ref_tax = sum((dec(r["tax"]) for r in refunds), Decimal(0))
    tot_tax = sum((dec(o["tax"]) for o in orders), Decimal(0))
    label = get_setting(c, "tax_label") or "Tax"
    summary = [{"item": f"{label} on sales", "value": money(tot_tax)}, {"item": f"− {label} on refunds", "value": money(ref_tax)},
               {"item": f"Net {label}", "value": money(tot_tax - ref_tax)}]
    return [
        {"title": f"{label} by rate", "columns": [col("rate", "Rate %", PCT), col("mode", "Prices"), col("checks", "Checks", INT),
                                                   col("taxable", "Taxable sales (ex tax)", MONEY), col("tax", label, MONEY), col("gross", "Incl. tax", MONEY)],
         "rows": rows, "totals": None},
        {"title": f"Net {label}", "columns": [col("item", "Line"), col("value", "Amount", MONEY)], "rows": summary, "totals": None},
    ], ["These receipts are not KRA eTIMS fiscal receipts. Confirm filings with the business's accountant."]


def _time_rows(orders, keyfn, keys_order=None):
    agg = OrderedDict((k, None) for k in (keys_order or []))
    for o in orders:
        k = keyfn(o)
        a = agg.get(k) or {"slot": k, "checks": 0, "guests": 0, "net": Decimal(0), "collected": Decimal(0)}
        a["checks"] += 1
        a["guests"] += o["guests"] or 0
        a["net"] += _ex_tax(o)
        a["collected"] += dec(o["total"])
        agg[k] = a
    total = sum((a["net"] for a in agg.values() if a), Decimal(0))
    rows = []
    for k, a in agg.items():
        a = a or {"slot": k, "checks": 0, "guests": 0, "net": Decimal(0), "collected": Decimal(0)}
        rows.append(dict(a, pct=_pct(a["net"], total), avg=a["collected"] / a["checks"] if a["checks"] else None))
    tot = {"slot": "Total", "checks": sum(r["checks"] for r in rows), "guests": sum(r["guests"] for r in rows), "net": total,
           "collected": sum((r["collected"] for r in rows), Decimal(0)), "pct": 100.0 if rows else None}
    tot["avg"] = tot["collected"] / tot["checks"] if tot["checks"] else None
    return _finish(rows, ["net", "collected", "avg"]), _finish([tot], ["net", "collected", "avg"])[0]


def _time_cols(label):
    return [col("slot", label), col("checks", "Checks", INT), col("guests", "Guests", INT), col("net", "Net sales", MONEY),
            col("pct", "% of net", PCT), col("collected", "Collected", MONEY), col("avg", "Avg check", MONEY)]


def rpt_time_period(c, ids):
    orders = _orders(c, ids)
    hours = sorted({o["created_at"][11:13] for o in orders})
    rows, tot = _time_rows(orders, lambda o: f"{o['created_at'][11:13]}:00–{int(o['created_at'][11:13]) + 1:02d}:00",
                           [f"{h}:00–{int(h) + 1:02d}:00" for h in hours])
    return [{"title": "Sales by hour", "columns": _time_cols("Hour"), "rows": rows, "totals": tot,
             "note": "By the time the check was paid. Before refunds."}], []


def rpt_serving_period(c, ids):
    periods = parse_periods(get_setting(c, "serving_periods"))
    orders = _orders(c, ids)
    names = [p[0] for p in periods]
    rows, tot = _time_rows(orders, lambda o: period_of(o["created_at"][11:16], periods), names)
    return [{"title": "Sales by serving period", "columns": _time_cols("Serving period"), "rows": rows, "totals": tot,
             "note": "Periods are set in Setup → Settings. Before refunds."}], []


def rpt_cashier(c, ids):
    from .logic import shift_summary
    rows = []
    for r in c.execute(f"""SELECT sh.id, d.business_date FROM shifts sh JOIN days d ON d.id = sh.day_id
                           WHERE sh.day_id IN ({_ph(ids)}) ORDER BY d.business_date, sh.id""", ids).fetchall():
        s = shift_summary(c, r["id"])
        rows.append({"date": r["business_date"], "cashier": s["staff_name"], "opened": s["opened_at"][11:16],
                     "closed": (s["closed_at"] or "")[11:16] or "open", "float": dec(s["opening_float"]),
                     "cash_sales": dec(s["sales_by_method"]["cash"]), "cash_refunds": dec(s["refunds_by_method"]["cash"]),
                     "mpesa": dec(s["sales_by_method"]["mpesa"]), "card": dec(s["sales_by_method"]["card"]),
                     "expected": dec(s["expected_cash"] if s["expected_cash"] is not None else s["expected_cash_now"]),
                     "counted": dec(s["counted_cash"]) if s["counted_cash"] is not None else None,
                     "variance": dec(s["cash_variance"]) if s["cash_variance"] is not None else None, "notes": s["notes"] or ""})
    keys = ["float", "cash_sales", "cash_refunds", "mpesa", "card", "expected", "counted", "variance"]
    tot = {"date": "Total", "cashier": f"{len(rows)} shifts"}
    for k in keys:
        tot[k] = sum((r[k] for r in rows if r[k] is not None), Decimal(0))
    cols = [col("date", "Date"), col("cashier", "Cashier"), col("opened", "Opened"), col("closed", "Closed"), col("float", "Float", MONEY),
            col("cash_sales", "Cash sales", MONEY), col("cash_refunds", "Cash refunds", MONEY), col("expected", "Expected cash", MONEY),
            col("counted", "Counted", MONEY), col("variance", "Over / short", MONEY), col("mpesa", "M-Pesa", MONEY),
            col("card", "Card", MONEY), col("notes", "Note")]
    return [{"title": "Cash shifts", "columns": cols, "rows": _finish(rows, keys), "totals": _finish([tot], keys)[0],
             "note": "Expected cash = float + cash sales − cash refunds paid from that drawer."}], []


def rpt_guest_checks(c, ids):
    rows = c.execute(
        f"""SELECT o.*, d.business_date, s.name AS cashier,
                   (SELECT GROUP_CONCAT(qty || '× ' || name, ', ') FROM order_items WHERE order_id = o.id) AS items
            FROM orders o JOIN days d ON d.id = o.day_id JOIN staff s ON s.id = o.staff_id
            WHERE o.day_id IN ({_ph(ids)}) ORDER BY d.business_date, o.number LIMIT 5000""", ids).fetchall()
    out = [{"date": r["business_date"], "number": r["number"], "time": r["created_at"][11:16], "cashier": r["cashier"],
            "type": r["order_type"], "guests": r["guests"], "items": r["items"], "subtotal": r["subtotal"] or r["total"],
            "discount": r["discount"], "tax": r["tax"], "total": r["total"],
            "payment": {"cash": "Cash", "mpesa": "M-Pesa", "card": "Card"}[r["payment_method"]] + (f" {r['payment_ref']}" if r["payment_ref"] else ""),
            "status": "VOID" if r["status"] == "void" else ("part refunded" if 0 < (r["refunded"] or 0) < r["total"] else
                                                            "refunded" if r["refunded"] else "paid"),
            "refunded": r["refunded"]} for r in rows]
    paid = [r for r in out if r["status"] != "VOID"]
    tot = {"date": "Total", "number": f"{len(out)} checks", "subtotal": sum(dec(r["subtotal"]) for r in paid),
           "discount": sum(dec(r["discount"]) for r in paid), "tax": sum(dec(r["tax"]) for r in paid),
           "total": sum(dec(r["total"]) for r in paid), "refunded": sum(dec(r["refunded"]) for r in paid)}
    keys = ["subtotal", "discount", "tax", "total", "refunded"]
    cols = [col("date", "Date"), col("number", "Check #"), col("time", "Time"), col("cashier", "Cashier"), col("type", "Type"),
            col("guests", "Guests", INT), col("items", "Items"), col("subtotal", "Gross", MONEY), col("discount", "Discount", MONEY),
            col("tax", "Tax", MONEY), col("total", "Total", MONEY), col("payment", "Payment"), col("status", "Status"),
            col("refunded", "Refunded", MONEY)]
    return [{"title": "Guest check journal", "columns": cols, "rows": _finish(out, keys), "totals": _finish([tot], keys)[0],
             "note": "Totals exclude voided checks."}], []


def rpt_employee(c, ids):
    orders = _orders(c, ids)
    voids = _orders(c, ids, "void")
    refunds = _refunds(c, ids)
    staff = OrderedDict()

    def s(name):
        return staff.setdefault(name, {"employee": name, "checks": 0, "guests": 0, "gross": Decimal(0), "discount": Decimal(0),
                                       "net": Decimal(0), "collected": Decimal(0), "voids": 0, "void_amt": Decimal(0),
                                       "refunds": 0, "refund_amt": Decimal(0), "disc_approved": Decimal(0)})
    names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM staff")}
    by_type = OrderedDict()
    for o in orders:
        a = s(o["cashier"])
        a["checks"] += 1
        a["guests"] += o["guests"] or 0
        a["gross"] += dec(o["subtotal"] or o["total"])
        a["discount"] += dec(o["discount"])
        a["net"] += _ex_tax(o)
        a["collected"] += dec(o["total"])
        if o["discount"] and o["discount_by"]:
            s(names.get(o["discount_by"], "?"))["disc_approved"] += dec(o["discount"])
        t = by_type.setdefault((o["cashier"], o["order_type"]), {"employee": o["cashier"], "type": o["order_type"], "checks": 0,
                                                                  "guests": 0, "net": Decimal(0), "collected": Decimal(0)})
        t["checks"] += 1
        t["guests"] += o["guests"] or 0
        t["net"] += _ex_tax(o)
        t["collected"] += dec(o["total"])
    for o in voids:
        a = s(o["cashier"])
        a["voids"] += 1
        a["void_amt"] += dec(o["total"])
    for r in refunds:
        a = s(r["staff"])
        a["refunds"] += 1
        a["refund_amt"] += dec(r["amount"])
    rows = [dict(a, avg=a["collected"] / a["checks"] if a["checks"] else None) for a in staff.values()]
    keys = ["gross", "discount", "net", "collected", "avg", "void_amt", "refund_amt", "disc_approved"]
    tot = {"employee": "Total"}
    for k in ["checks", "guests", "voids", "refunds"]:
        tot[k] = sum(r[k] for r in rows)
    for k in keys:
        tot[k] = sum((r[k] for r in rows if r[k] is not None), Decimal(0))
    tot["avg"] = tot["collected"] / tot["checks"] if tot["checks"] else None
    cols = [col("employee", "Employee"), col("checks", "Checks", INT), col("guests", "Guests", INT), col("gross", "Gross", MONEY),
            col("discount", "Discounts on own checks", MONEY), col("net", "Net sales", MONEY), col("collected", "Collected", MONEY),
            col("avg", "Avg check", MONEY), col("voids", "Voided checks", INT), col("void_amt", "Voided amount", MONEY),
            col("refunds", "Refunds given", INT), col("refund_amt", "Refunded", MONEY), col("disc_approved", "Discounts approved", MONEY)]
    t_rows = _finish([dict(t, avg=t["collected"] / t["checks"]) for t in by_type.values()], ["net", "collected", "avg"])
    return [
        {"title": "Employee sales", "columns": cols, "rows": _finish(rows, keys), "totals": _finish([tot], keys)[0],
         "note": "Voids are counted against the person who rang the check; refunds against the person who gave them."},
        {"title": "Employee by order type", "columns": [col("employee", "Employee"), col("type", "Order type"), col("checks", "Checks", INT),
                                                       col("guests", "Guests", INT), col("net", "Net sales", MONEY),
                                                       col("collected", "Collected", MONEY), col("avg", "Avg check", MONEY)],
         "rows": t_rows, "totals": None},
    ], []


def rpt_major_group(c, ids):
    lines, ret = _lines(c, ids)
    return [_group_sales(lines, ret, "major", "Major group")], ["Major groups are set per category in Setup → Categories."]


def rpt_family_group(c, ids):
    lines, ret = _lines(c, ids)
    return [_group_sales(lines, ret, "family", "Category (family group)")], []


def rpt_menu_item(c, ids):
    lines, ret = _lines(c, ids)
    agg = OrderedDict()
    blank = {"qty": 0, "ret_qty": 0, "gross": Decimal(0), "discount": Decimal(0), "net": Decimal(0), "returns": Decimal(0), "cost": Decimal(0)}
    for l in lines:
        a = agg.setdefault(l["menu_item_id"], dict(blank, item=l["name"], family=l["family"]))
        a["qty"] += l["qty"]
        a["gross"] += l["gross"]
        a["discount"] += l["discount"]
        a["net"] += l["net"]
        a["cost"] += l["cost"]
    for r in ret:
        a = agg.setdefault(r["menu_item_id"], dict(blank, item=r["name"], family=r["family"]))
        a["ret_qty"] += r["qty"]
        a["returns"] += r["net"]
    total = sum((a["net"] - a["returns"] for a in agg.values()), Decimal(0))
    rows = []
    for a in agg.values():
        net = a["net"] - a["returns"]
        sold = a["qty"] - a["ret_qty"]
        rows.append({"item": a["item"], "family": a["family"], "qty": a["qty"], "ret_qty": a["ret_qty"], "gross": a["gross"],
                     "discount": a["discount"], "returns": a["returns"], "net": net, "pct": _pct(net, total),
                     "avg": net / sold if sold > 0 else None, "cost": a["cost"],
                     "margin": net - a["cost"], "cost_pct": _pct(a["cost"], net) if net > 0 else None})
    rows.sort(key=lambda r: -r["net"])
    keys = ["gross", "discount", "returns", "net", "avg", "cost", "margin"]
    tot = {"item": "Total", "qty": sum(r["qty"] for r in rows), "ret_qty": sum(r["ret_qty"] for r in rows), "pct": 100.0 if rows else None}
    for k in ["gross", "discount", "returns", "net", "cost", "margin"]:
        tot[k] = sum((r[k] for r in rows), Decimal(0))
    tot["cost_pct"] = _pct(tot["cost"], tot["net"]) if tot["net"] > 0 else None
    cols = [col("item", "Menu item"), col("family", "Category"), col("qty", "Qty sold", INT), col("ret_qty", "Returned", INT),
            col("gross", "Gross", MONEY), col("discount", "Discounts", MONEY), col("returns", "Returns", MONEY),
            col("net", "Net sales", MONEY), col("pct", "% of net", PCT), col("avg", "Avg net price", MONEY),
            col("cost", "Food cost", MONEY), col("cost_pct", "Food cost %", PCT), col("margin", "Gross margin", MONEY)]
    return [{"title": "Menu item sales", "columns": cols, "rows": _finish(rows, keys), "totals": _finish([tot], keys)[0],
             "note": "Food cost = recipe quantities × ingredient cost at the time of sale (less restocked returns). "
                     "Items without a recipe show 0 cost."}], []


def rpt_discounts(c, ids):
    rows = c.execute(
        f"""SELECT d.business_date, o.number, o.created_at, s.name AS cashier, a.name AS approver, o.discount_reason,
                   o.subtotal, o.discount, o.status
            FROM orders o JOIN days d ON d.id = o.day_id JOIN staff s ON s.id = o.staff_id LEFT JOIN staff a ON a.id = o.discount_by
            WHERE o.day_id IN ({_ph(ids)}) AND o.discount > 0 ORDER BY d.business_date, o.number""", ids).fetchall()
    out = [{"date": r["business_date"], "number": r["number"], "time": r["created_at"][11:16], "cashier": r["cashier"],
            "approver": r["approver"] or "", "reason": r["discount_reason"] or "", "subtotal": r["subtotal"], "discount": r["discount"],
            "pct": _pct(dec(r["discount"]), dec(r["subtotal"])), "status": r["status"]} for r in rows]
    by = OrderedDict()
    for r in out:
        if r["status"] != "paid":
            continue
        b = by.setdefault(r["reason"] or "(none)", {"reason": r["reason"] or "(none)", "count": 0, "discount": Decimal(0)})
        b["count"] += 1
        b["discount"] += dec(r["discount"])
    paid = [r for r in out if r["status"] == "paid"]
    tot = {"date": "Total", "number": f"{len(paid)} checks", "subtotal": sum(dec(r["subtotal"]) for r in paid),
           "discount": sum(dec(r["discount"]) for r in paid)}
    tot["pct"] = _pct(tot["discount"], tot["subtotal"])
    cols = [col("date", "Date"), col("number", "Check #"), col("time", "Time"), col("cashier", "Cashier"), col("approver", "Approved by"),
            col("reason", "Reason"), col("subtotal", "Check gross", MONEY), col("discount", "Discount", MONEY), col("pct", "%", PCT),
            col("status", "Status")]
    return [
        {"title": "Discounts by reason", "columns": [col("reason", "Reason"), col("count", "Checks", INT), col("discount", "Discount", MONEY)],
         "rows": _finish(sorted(by.values(), key=lambda b: -b["discount"]), ["discount"]), "totals": None},
        {"title": "Discount detail", "columns": cols, "rows": _finish(out, ["subtotal", "discount"]),
         "totals": _finish([tot], ["subtotal", "discount"])[0], "note": "Totals exclude discounts on voided checks."},
    ], []


def rpt_voids(c, ids):
    rows = c.execute(
        f"""SELECT d.business_date, o.number, o.created_at, s.name AS cashier, v.name AS voided_by, o.void_reason, o.total,
                   (SELECT GROUP_CONCAT(qty || '× ' || name, ', ') FROM order_items WHERE order_id = o.id) AS items
            FROM orders o JOIN days d ON d.id = o.day_id JOIN staff s ON s.id = o.staff_id LEFT JOIN staff v ON v.id = o.voided_by
            WHERE o.day_id IN ({_ph(ids)}) AND o.status = 'void' ORDER BY d.business_date, o.number""", ids).fetchall()
    out = [{"date": r["business_date"], "number": r["number"], "time": r["created_at"][11:16], "cashier": r["cashier"],
            "by": r["voided_by"] or "", "reason": r["void_reason"] or "", "items": r["items"], "total": r["total"]} for r in rows]
    by = OrderedDict()
    for r in out:
        b = by.setdefault(r["reason"], {"reason": r["reason"], "count": 0, "total": Decimal(0)})
        b["count"] += 1
        b["total"] += dec(r["total"])
    cols = [col("date", "Date"), col("number", "Check #"), col("time", "Rung at"), col("cashier", "Rung by"), col("by", "Voided by"),
            col("reason", "Reason"), col("items", "Items"), col("total", "Amount", MONEY)]
    tot = {"date": "Total", "number": f"{len(out)} checks", "total": sum((dec(r["total"]) for r in out), Decimal(0))}
    return [
        {"title": "Voids by reason", "columns": [col("reason", "Reason"), col("count", "Checks", INT), col("total", "Amount", MONEY)],
         "rows": _finish(sorted(by.values(), key=lambda b: -b["total"]), ["total"]), "totals": None},
        {"title": "Void detail", "columns": cols, "rows": _finish(out, ["total"]), "totals": _finish([tot], ["total"])[0],
         "note": "Voids cancel a whole check on the same day and put its ingredients back in stock."},
    ], []


def rpt_refunds(c, ids):
    refunds = _refunds(c, ids)
    items = {r["refund_id"]: r["items"] for r in c.execute(
        f"""SELECT rl.refund_id, GROUP_CONCAT(rl.qty || '× ' || oi.name, ', ') items FROM refund_lines rl
            JOIN order_items oi ON oi.id = rl.order_item_id JOIN refunds r ON r.id = rl.refund_id
            WHERE r.day_id IN ({_ph(ids)}) GROUP BY rl.refund_id""", ids)}
    out = [{"date": r["business_date"], "check": f"{r['order_date']} #{r['number']}", "time": r["created_at"][11:16],
            "staff": r["staff"], "approver": r["approver"] or "", "reason": r["reason"], "items": items.get(r["id"], ""),
            "method": {"cash": "Cash", "mpesa": "M-Pesa", "card": "Card"}[r["method"]], "restocked": "yes" if r["restocked"] else "no",
            "amount": r["amount"]} for r in refunds]
    by = OrderedDict()
    for r in out:
        b = by.setdefault(r["reason"], {"reason": r["reason"], "count": 0, "amount": Decimal(0)})
        b["count"] += 1
        b["amount"] += dec(r["amount"])
    cols = [col("date", "Refund date"), col("check", "Original check"), col("time", "Time"), col("staff", "Given by"),
            col("approver", "Approved by"), col("reason", "Reason"), col("items", "Items"), col("method", "Paid back by"),
            col("restocked", "Restocked"), col("amount", "Amount", MONEY)]
    tot = {"date": "Total", "check": f"{len(out)} refunds", "amount": sum((dec(r["amount"]) for r in out), Decimal(0))}
    return [
        {"title": "Refunds by reason", "columns": [col("reason", "Reason"), col("count", "Refunds", INT), col("amount", "Amount", MONEY)],
         "rows": _finish(sorted(by.values(), key=lambda b: -b["amount"]), ["amount"]), "totals": None},
        {"title": "Refund detail", "columns": cols, "rows": _finish(out, ["amount"]), "totals": _finish([tot], ["amount"])[0]},
    ], []


# key: (group, title, function) — groups mirror the MICROS report menu
CATALOG = OrderedDict([
    ("sales", ("Sales balance", "Sales detail (tenders, order types, daily)", rpt_sales)),
    ("tax", ("Sales balance", "Tax", rpt_tax)),
    ("time_period", ("Period", "Time period (hourly)", rpt_time_period)),
    ("serving_period", ("Period", "Serving period", rpt_serving_period)),
    ("cashier", ("Cash management", "Cashier shifts (over / short)", rpt_cashier)),
    ("guest_checks", ("Guest check", "Guest check journal", rpt_guest_checks)),
    ("employee", ("Employee", "Employee sales & order types", rpt_employee)),
    ("major_group", ("Group", "Major group", rpt_major_group)),
    ("family_group", ("Group", "Category (family group)", rpt_family_group)),
    ("menu_item", ("Menu item", "Menu item sales & food cost", rpt_menu_item)),
    ("discounts", ("Reason codes", "Discounts", rpt_discounts)),
    ("voids", ("Reason codes", "Voids", rpt_voids)),
    ("refunds", ("Reason codes", "Refunds / returns", rpt_refunds)),
])

# The end-of-day pack ("autosequence"): what a manager prints or files after closing.
EOD_PACK = ["sales", "tax", "cashier", "employee", "family_group", "menu_item", "discounts", "voids", "refunds"]


def run(c, key, date_from, date_to):
    if key not in CATALOG:
        raise ValueError("Unknown report.")
    group, title, fn = CATALOG[key]
    ids = _days_between(c, date_from, date_to)  # validates both dates
    if date_from > date_to:
        raise ValueError("'From' must be on or before 'To'.")
    sections, notes = fn(c, ids)
    if not ids:
        notes = ["No business days in this date range."] + notes
    imported = c.execute(f"SELECT COUNT(*) FROM days WHERE id IN ({_ph(ids)}) AND source = 'workbook'", ids).fetchone()[0] if ids else 0
    if imported:
        notes.append(f"{imported} imported workbook day(s) in range have stock history only; their sales were on the other POS.")
    return {"key": key, "group": group, "title": title, "from": date_from, "to": date_to, "days": len(ids),
            "business_name": get_setting(c, "business_name"), "sections": sections, "notes": notes}


def to_csv(report):
    """All sections in one CSV, formula-safe (same rule as importer.to_csv)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)

    def safe(v):
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return "" if v is None else v
    w.writerow([report["business_name"], report["title"], f"{report['from']} to {report['to']}", f"{report['days']} day(s)"])
    for s in report["sections"]:
        w.writerow([])
        w.writerow([s["title"]])
        w.writerow([cdef["label"] for cdef in s["columns"]])
        for r in s["rows"] + ([s["totals"]] if s.get("totals") else []):
            w.writerow([safe(r.get(cdef["key"])) for cdef in s["columns"]])
        if s.get("note"):
            w.writerow([safe("Note: " + s["note"])])
    for n in report["notes"]:
        w.writerow([safe("Note: " + n)])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _fmt(v, t, row=None):
    if t == "auto":
        t = (row or {}).get("_type", TEXT)
    if v is None or v == "":
        return "–" if t != TEXT else ""
    if t == MONEY:
        return f"{v:,.2f}"
    if t == PCT:
        return f"{v:g}%"
    if t == INT:
        return f"{int(v):,}"
    return str(v)


def to_html(report_list, title="Reports"):
    """Self-contained printable HTML (used by the end-of-day pack script)."""
    from html import escape as e
    out = [f"<!doctype html><meta charset='utf-8'><title>{e(title)}</title><style>"
           "body{font:12px system-ui,sans-serif;margin:16px}h1{font-size:18px}h2{font-size:15px;margin:22px 0 4px;page-break-before:always}"
           "h2.first{page-break-before:auto}h3{font-size:13px;margin:12px 0 4px}table{border-collapse:collapse;width:100%}"
           "th,td{border-bottom:1px solid #ddd;padding:3px 6px;text-align:left}td.n,th.n{text-align:right;white-space:nowrap}"
           "tr.t td{font-weight:700;border-top:2px solid #000}.note{color:#555;font-size:11px}</style>"]
    for i, rep in enumerate(report_list):
        if i == 0:
            out.append(f"<h1>{e(rep['business_name'])} · {e(title)}</h1>")
        out.append(f"<h2 class='{'first' if i == 0 else ''}'>{e(rep['title'])} · {e(rep['from'])}"
                   f"{'' if rep['from'] == rep['to'] else ' to ' + e(rep['to'])}</h2>")
        for s in rep["sections"]:
            out.append(f"<h3>{e(s['title'])}</h3><table><tr>")
            out += [f"<th class='{'' if cdef['type'] == TEXT else 'n'}'>{e(cdef['label'])}</th>" for cdef in s["columns"]]
            out.append("</tr>")
            for r, cls in [(r, "") for r in s["rows"]] + ([(s["totals"], "t")] if s.get("totals") else []):
                out.append(f"<tr class='{cls}'>" + "".join(
                    f"<td class='{'' if cdef['type'] == TEXT else 'n'}'>{e(_fmt(r.get(cdef['key']), cdef['type'], r))}</td>"
                    for cdef in s["columns"]) + "</tr>")
            out.append("</table>")
            if s.get("note"):
                out.append(f"<div class='note'>{e(s['note'])}</div>")
        out += [f"<div class='note'>{e(n)}</div>" for n in rep["notes"]]
    return "".join(out)
