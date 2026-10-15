# CFC POS + Smart Inventory

A point-of-sale system and raw-material inventory for **City Food Court / House of Mandi**, Kisumu.
It runs **fully offline on the restaurant's LAN**: one small server, and every till, tablet or office PC opens it in a browser.

Every sale automatically deducts its raw materials using the recipe. At closing, staff count what is physically left and the system shows the difference:

```
expected closing = opening + delivered − used by sales − recorded waste
difference       = counted − expected
```

A difference bigger than the tolerance (default 2%, and any difference at all for items counted in pieces) **can't be saved without a reason**. Tonight's count becomes tomorrow's opening stock.

## Features (v0.1)

| Area | What it does |
|---|---|
| POS | Touch menu by category, cart, cash (with change) / M-Pesa (code check) / card, 80 mm printable receipt, reprint |
| Orders | Today's orders; manager-only void with reason (stock is returned automatically) |
| Stock | Live expected stock, reorder warnings, record waste (any staff) and deliveries (manager) |
| Open day | First day: full count. After that, carried over from last night with the option to correct |
| Close day | Count, live difference per ingredient, reason + note, blocked until every difference over tolerance is explained |
| Reports | Sales by payment method, items sold, full reconciliation with KES value of shortages, waste, voids; printable |
| Setup | Menu items + recipes (with food-cost %), ingredients (kg/L/pcs, cost, reorder level, daily/weekly count), staff PINs and roles, settings |
| Excel | Download/upload ingredients + recipes; the import is all-or-nothing with row-level error messages |
| Audit | Every stock change is a separate row with who/when/why; nothing is overwritten |

Roles: **cashier** (sell, waste, view stock) · **manager** (+ open/close, deliveries, voids, reports, menu) · **admin** (+ staff, settings).

## Stack

FastAPI + SQLite (one file: `data/cfc.db`) + plain HTML/JS (no build step, no CDN, nothing loaded from the internet).

```
app/main.py      HTTP API + serves the UI
app/logic.py     business rules: selling, stock movements, reconciliation, reports
app/db.py        schema
app/importer.py  Excel import/export
app/seed.py      demo data
static/          UI (index.html, app.js, style.css)
scripts/backup.py nightly database backup
tests/           end-to-end test of a full business day
```

## Run it

**Windows (restaurant PC):** install Python 3.10+ from python.org (tick *Add to PATH*), then double-click `setup.bat` once, and `run.bat` to start.

**Linux / Mac:**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python3 -m app.seed          # optional demo data
./run.sh
```

Open `http://localhost:8000` on the server, or `http://<server-ip>:8000` on any device on the same network.
First login: **admin PIN 1234**. Change it under Setup → Staff. The demo data also adds manager `2222` and cashier `1111`.

Tests: `pip install -r requirements-dev.txt && pytest -q`

## The server

Any always-on computer on the restaurant network works: a mini PC, a refurbished desktop, or the back-office PC for the pilot. Give it a **fixed IP** on the router so tablets always find it, plug it into a **UPS**, and run `python scripts/backup.py` nightly (Task Scheduler / cron), copying `data/backups/` off the machine regularly.

## Loading CFC's real data

1. Setup → Excel → *Download* (gives the template with a *How to fill* sheet).
2. Fill **Ingredients** (name, unit g/ml/pcs or kg/L, cost, reorder level, daily/weekly count) and **Recipes** (one row per ingredient per dish, raw quantity for one plate).
3. Upload. Nothing is saved if any row has a problem.

If CFC's existing sheet has a different layout, write an adapter in `app/importer.py` rather than retyping.

## Roadmap / known gaps

- [ ] **Batch / prep recipes**: rice and meat cooked in pots (raw → batch → portions) and yield factors
- [ ] **Modifiers**: extra chicken, large portion, no onions
- [ ] Kitchen tickets (second printer / kitchen screen) and direct ESC/POS printing
- [ ] **KRA eTIMS** integration (required before this replaces the Oracle POS)
- [ ] M-Pesa Daraja STK push + automatic confirmation (needs internet; currently the code is typed in)
- [ ] Split payments, discounts, table/tab orders
- [ ] Supplier records and purchase orders; weekly stock-take flow for "weekly" items
- [ ] Optional cloud sync so the owner can see reports remotely
- [ ] Shift cash-up (expected vs counted cash per cashier)

## Pilot plan

Run alongside the Oracle POS on one till/at quiet hours. Load the 10–15 best-selling dishes and the ~20 most expensive ingredients first, close the day every night for 2 weeks, then compare against the manual Excel process.

---
© 2026 Elisha Gilbert. Private repository, all rights reserved.
