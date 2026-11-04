"""Inventory analytics for the dashboards: what was used, bought, wasted and lost, and what runs out next.

Everything is derived from data the system already holds (day_stock lines, stock movements, purchase
orders); nothing here has to be typed in. Money is valued at each item's current cost per unit, so
values for items without a cost are 0 and the response says how many items are costed.

Usage per item and day:
  closed day: opening + delivered + adjusted - counted   (what physically left the store)
  open day:   sales + waste recorded so far
Shortage = a negative closing variance (less on the shelf than the records say should be there).
"""
from datetime import date, timedelta

from .logic import day_lines, money, open_day_row, stock_overview


def _r(v, n=3):
    return round(v or 0, n)


def build(c, days=30, include_money=True):
    days = max(1, min(int(days), 366))
    end = date.today()
    start = end - timedelta(days=days - 1)
    s0, s1 = start.isoformat(), end.isoformat()

    items = {r["id"]: dict(r) for r in c.execute("SELECT id, name, unit, cost_per_unit, category, active FROM ingredients")}
    cost = {i: (r["cost_per_unit"] or 0) for i, r in items.items()}
    day_rows = [dict(r) for r in c.execute(
        """SELECT id, business_date, covers_to, status, source FROM days
           WHERE COALESCE(covers_to, business_date) >= ? AND business_date <= ? ORDER BY business_date""", (s0, s1))]
    open_day = open_day_row(c)

    daily, usage_by_item, short_by_item = [], {}, {}
    for d in day_rows:
        if d["status"] == "closed":
            lines = [dict(r) for r in c.execute(
                """SELECT ingredient_id AS id, opening, COALESCE(delivered,0) AS delivered, COALESCE(adjusted,0) AS adjusted,
                          COALESCE(sold,0) AS sold, COALESCE(wasted,0) AS wasted, counted, variance
                   FROM day_stock WHERE day_id = ?""", (d["id"],))]
        else:
            lines = [{"id": ln["id"], "opening": ln["opening"], "delivered": ln["delivered"], "adjusted": ln["adjusted"],
                      "sold": ln["sold"], "wasted": ln["wasted"], "counted": None, "variance": None}
                     for ln in day_lines(c, d["id"])]
        used_v = deliv_v = waste_v = short_v = 0.0
        short_n = 0
        for ln in lines:
            i = ln["id"]
            if i not in items:
                continue
            used = (ln["opening"] + ln["delivered"] + ln["adjusted"] - ln["counted"]) if ln["counted"] is not None \
                else (ln["sold"] + ln["wasted"])
            used = max(used, 0)
            k = cost[i]
            used_v += used * k
            deliv_v += max(ln["delivered"], 0) * k
            waste_v += max(ln["wasted"], 0) * k
            u = usage_by_item.setdefault(i, [0.0, 0])
            u[0] += used
            u[1] += 1
            var = ln["variance"]
            if var is not None and var < -1e-9:
                short_v += -var * k
                short_n += 1
                sh = short_by_item.setdefault(i, [0.0, 0])
                sh[0] += -var
                sh[1] += 1
        daily.append({"date": d["business_date"], "covers_to": d["covers_to"], "status": d["status"], "source": d["source"],
                      "usage_value": money(used_v), "delivered_value": money(deliv_v), "waste_value": money(waste_v),
                      "shortage_value": money(short_v), "items_short": short_n})

    # Waste by reason (from the movements, so it includes today).
    waste = [dict(r) for r in c.execute(
        """SELECT COALESCE(m.reason, 'Not given') AS reason, COUNT(*) AS entries, SUM(-m.qty * COALESCE(i.cost_per_unit, 0)) AS value
           FROM stock_movements m JOIN days d ON d.id = m.day_id JOIN ingredients i ON i.id = m.ingredient_id
           WHERE m.type = 'waste' AND d.business_date BETWEEN ? AND ? GROUP BY COALESCE(m.reason, 'Not given')
           ORDER BY value DESC, entries DESC""", (s0, s1))]
    for w in waste:
        w["value"] = money(w["value"])

    # Purchases received by supplier (PO deliveries carry the supplier; the rest were direct deliveries).
    suppliers = [dict(r) for r in c.execute(
        """SELECT COALESCE(s.name, 'Direct deliveries') AS name,
                  SUM(m.qty * COALESCE(m.unit_cost, i.cost_per_unit, 0)) AS value, COUNT(*) AS deliveries
           FROM stock_movements m JOIN days d ON d.id = m.day_id JOIN ingredients i ON i.id = m.ingredient_id
           LEFT JOIN po_lines pl ON pl.id = m.po_line_id LEFT JOIN purchase_orders po ON po.id = pl.po_id
           LEFT JOIN suppliers s ON s.id = po.supplier_id
           WHERE m.type = 'delivery' AND d.business_date BETWEEN ? AND ?
           GROUP BY COALESCE(s.name, 'Direct deliveries') ORDER BY value DESC""", (s0, s1))]
    for s in suppliers:
        s["value"] = money(s["value"])

    def ranked(src, key):
        out = []
        for i, (qty, n) in src.items():
            it = items[i]
            out.append({"id": i, "name": it["name"], "unit": it["unit"], "qty": _r(qty), "days": n,
                        "value": money(qty * cost[i])})
        out.sort(key=lambda x: (x["value"], x["qty"]) if key == "value" else (x["qty"],), reverse=True)
        return out

    top_usage = ranked(usage_by_item, "value")[:10]
    shortages = ranked(short_by_item, "value")[:10]

    overview = stock_overview(c)
    active = [o for o in overview if o["active"]]
    status_counts = {k: 0 for k in ("out", "critical", "low", "healthy", "inactive")}
    for o in overview:
        status_counts[o["status"]] += 1
    cover = []
    for o in active:
        if o["avg_daily_usage"]:
            left = max(o["on_hand"], 0) / o["avg_daily_usage"]
            cover.append({"id": o["id"], "name": o["name"], "unit": o["unit"], "on_hand": _r(o["on_hand"]),
                          "avg_daily_usage": o["avg_daily_usage"], "days_left": round(left, 1), "status": o["status"],
                          "incoming": o["incoming"]})
    cover.sort(key=lambda x: x["days_left"])
    categories = {}
    for o in active:
        k = o["category"] or "Uncategorised"
        cat = categories.setdefault(k, {"name": k, "value": 0.0, "items": 0})
        cat["value"] += o["value"]
        cat["items"] += 1
    categories = sorted(({**v, "value": money(v["value"])} for v in categories.values()), key=lambda x: -x["value"])

    totals = {
        "usage_value": money(sum(d["usage_value"] for d in daily)),
        "delivered_value": money(sum(d["delivered_value"] for d in daily)),
        "waste_value": money(sum(w["value"] for w in waste)),
        "shortage_value": money(sum(d["shortage_value"] for d in daily)),
        "stock_value": money(sum(o["value"] for o in active)),
        "days_recorded": len(daily),
        "closed_days": sum(1 for d in daily if d["status"] == "closed"),
    }
    totals["waste_pct_of_usage"] = round(100 * totals["waste_value"] / totals["usage_value"], 1) if totals["usage_value"] else None
    totals["shortage_pct_of_usage"] = (round(100 * totals["shortage_value"] / totals["usage_value"], 1)
                                       if totals["usage_value"] else None)
    coverage = {"items": len(active), "costed": sum(1 for o in active if o["cost_per_unit"]),
                "with_reorder_level": sum(1 for o in active if o["par_level"])}

    out = {
        "range": {"from": s0, "to": s1, "days": days},
        "money": include_money,
        "open_day": dict(open_day) if open_day else None,
        "coverage": coverage,
        "totals": totals,
        "daily": daily,
        "waste_by_reason": waste,
        "top_usage": top_usage,
        "shortages": shortages,
        "suppliers": suppliers,
        "cover": cover[:12],
        "categories": categories,
        "status_counts": status_counts,
        "insights": insights(totals, shortages, cover, waste, coverage, status_counts, include_money),
    }
    if not include_money:
        _strip_money(out)
    return out


def _strip_money(out):
    for d in out["daily"]:
        for k in ("usage_value", "delivered_value", "waste_value", "shortage_value"):
            d.pop(k, None)
    for k in ("waste_by_reason", "top_usage", "shortages", "suppliers", "categories"):
        for r in out[k]:
            r.pop("value", None)
    out["suppliers"] = []
    out["categories"] = [{"name": r["name"], "items": r["items"]} for r in out["categories"]]
    out["totals"] = {k: v for k, v in out["totals"].items() if k in ("days_recorded", "closed_days")}


def _qty(q, unit):
    if unit == "g" and abs(q) >= 1000:
        return f"{q / 1000:,.1f} kg"
    if unit == "ml" and abs(q) >= 1000:
        return f"{q / 1000:,.1f} L"
    return f"{q:,.0f} {unit}"


def insights(totals, shortages, cover, waste, coverage, status, include_money):
    """Short factual findings, most important first. Each is something a manager can act on."""
    out = []
    soon = [x for x in cover if x["days_left"] <= 2 and not x["incoming"]]
    if soon:
        names = ", ".join(f"{x['name']} (~{x['days_left']:g} days)" for x in soon[:3])
        out.append({"level": "bad", "text": f"Runs out within two days at the current rate, with nothing on order: {names}."})
    if status["out"]:
        out.append({"level": "bad", "text": f"{status['out']} item{'s are' if status['out'] != 1 else ' is'} out of stock."})
    if shortages:
        s = shortages[0]
        money_part = f", worth KES {s['value']:,.0f}" if include_money and s["value"] else ""
        out.append({"level": "warn", "text": f"Largest unexplained loss: {s['name']}, {_qty(s['qty'], s['unit'])} short "
                    f"over {s['days']} day{'s' if s['days'] != 1 else ''}{money_part}."})
    if include_money and totals.get("waste_pct_of_usage") is not None and totals["waste_pct_of_usage"] >= 3:
        top = waste[0]["reason"] if waste else None
        out.append({"level": "warn", "text": f"Waste is {totals['waste_pct_of_usage']:g}% of stock used"
                    + (f"; most of it is recorded as '{top}'." if top else ".")})
    if coverage["items"] and coverage["costed"] < coverage["items"]:
        missing = coverage["items"] - coverage["costed"]
        out.append({"level": "info", "text": f"{missing} of {coverage['items']} items have no purchase cost, so values "
                    "for them show as zero. Costs fill in automatically when deliveries are received with a price."})
    if coverage["items"] and coverage["with_reorder_level"] < coverage["items"]:
        missing = coverage["items"] - coverage["with_reorder_level"]
        out.append({"level": "info", "text": f"{missing} items have no reorder level, so they can't be flagged as low."})
    return out[:6]
