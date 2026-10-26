# CFC INV-POS — Point of Sale + Smart Inventory

POS and inventory system for **City Food Court / House of Mandi**, Kisumu.
It runs on the restaurant's **local network (LAN)**: one small server PC, and every till, Android tablet or office PC opens it in a browser. No internet connection is needed to sell.

Every sale deducts its raw materials through the recipe, so stock is always "what should be there". At closing, staff count what is physically left and the system shows the difference:

```
expected closing = opening + received + adjustments − used by sales (net of voids / restocked refunds) − waste
difference       = counted − expected        (negative = more left the store than was sold)
```

This is the same formula as CFC's daily stock-movement workbook (`variance = system sales − actual sales`), so the workbook's history imports without sign changes and its last closing count becomes the system's opening stock.

## Features (v0.3)

| Area | What it does |
|---|---|
| **POS** | Order type per check (Dine in / Takeaway / Delivery, configurable), optional guest count, touch menu + barcode/SKU/name search (scanner = keyboard), cart, discounts (manager PIN above a set %), configurable tax (inclusive/exclusive), cash with change / M-Pesa code / card, 80 mm receipt, keyboard shortcuts for Windows tills |
| **Safe checkout** | Every checkout has an idempotency key: a retry after a network drop returns the original sale instead of charging twice. Receipt numbers and stock deductions are written in one locked transaction |
| **Cash shifts** | Open with a float, close with a count; expected cash = float + cash sales − cash refunds; over/short needs a note |
| **Voids & refunds** | Same-day void (manager); partial/full refunds at any later date with manager approval, optional restock, exact-cent final refund |
| **Stock** | Live on-hand per item, colour status (see below), waste, receiving, manager adjustments, full movement history with who/when/why |
| **Purchasing** | Suppliers, purchase orders (draft → ordered → partial → received / closed short), partial deliveries, receiving updates stock only when goods arrive; optional last-cost or weighted-average costing |
| **Open/close day** | Carry-over from last count, closing count with tolerance and mandatory reasons, blocked while cash shifts are open |
| **Reports (MICROS-style)** | Sales detail, tax, hourly and serving-period sales, cashier over/short, guest check journal, employee sales by order type, major/family group, menu item sales with food cost, discount/void/refund reason reports; daily or any date range; print, CSV and an end-of-day pack. See [docs/MICROS_PARITY.md](docs/MICROS_PARITY.md) |
| **Other reports** | Day report; period report with gross sales, discounts, refunds, tax, net sales, **COGS** (cost snapshotted at sale time), **gross profit** and margin, purchases by supplier, waste, stock differences; stock valuation; CSV exports |
| **Import/export** | Recipe/ingredient template (.xlsx) and ingredient CSV with *Check file* preview, duplicate detection and all-or-nothing import; **daily stock-movement workbook importer** with unit confirmation and a discrepancy report |
| **Security** | Hashed PINs + server pepper, login throttling, session expiry, forced PIN change for default/weak PINs, 4 roles enforced on the server, audit log, CSP and security headers, request size limits |
| **Licensing** | Offline Ed25519-signed licences: activation, expiry + grace, renewal, revocation lists, device limits. **Development mode by default — never blocks** |

### Stock colours (used everywhere, always with a text label)

| Colour | Meaning |
|---|---|
| 🔴 Red — *Out of stock / Critical* | on hand ≤ 0, or ≤ the item's critical level (default: 50 % of its reorder level) |
| 🟠 Amber — *Low stock* | on hand at or below the reorder level |
| 🟢 Green — *Healthy* | above the reorder level |
| 🔵 Blue — *Incoming* | ordered on a purchase order but not received. Shown next to the status; **never counted as stock** |
| ⚪ Grey — *Inactive* | archived / discontinued |

Reorder suggestions appear only when there are at least 3 closed days of real usage history.

### Roles

**Cashier** sell, own cash shift, record waste, see stock (no costs) · **Stock controller** suppliers, purchase orders, receive goods, waste (cannot sell) · **Manager** everything above + open/close day, voids, refunds, discounts, adjustments, menu & prices, reports, imports/exports · **Admin** + staff, settings, audit log, licence.

## Stack

FastAPI + SQLite (one file: `data/cfc.db`) + plain HTML/JS (no build step, nothing loaded from the internet). Chosen deliberately: it installs on any Windows PC, needs no database server, and Android tablets need only Chrome. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for why no native/desktop wrapper is needed.

```
app/main.py       HTTP API, auth, permissions, serves the UI
app/logic.py      business rules: selling, refunds, shifts, stock, purchasing, reconciliation, reports
app/db.py         schema + versioned migrations (auto-backup before upgrading)
app/security.py   PIN hashing, sessions, login throttle, permission table
app/importer.py   Excel/CSV import/export, workbook history import
app/reports.py    MICROS-style management reports (one engine: screen, print, CSV, end-of-day pack)
app/workbook.py   reader + discrepancy checks for the daily stock-movement workbook
app/licence.py    licence verification (development mode by default)
app/seed.py       demo data (refuses to touch a database with real days)
static/           UI (index.html, app.js, style.css, manifest)
scripts/          backup.py, eod_reports.py, reset_admin_pin.py, licence_tool.py
tests/            pytest suite (business workflows, security, licensing, workbook)
docs/             DEPLOYMENT.md, SECURITY.md, WORKBOOK_RECONCILIATION.md
```

## Run it

**Windows (restaurant server PC):** install Python 3.10+ from python.org (tick *Add to PATH*), double-click `setup.bat` once, then `run.bat`.

**Linux / Mac:**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

Open `http://localhost:8000` on the server, or `http://<server-ip>:8000` on any device on the same network.

**First login:** admin PIN **1234**. You are forced to choose a new PIN immediately.
Demo data (separate database only): `set CFC_DB=data\demo.db` then `python -m app.seed` → PINs admin 1234, manager 2222, cashier 1111, stock controller 3333.

**Upgrading an existing v0.1 database:** just start the new version. It backs up `data/cfc.db` to `data/backups/pre-migration-*.db`, hashes the PINs, and requires managers/admins with weak PINs (1234, 2222…) to change them at next login. Everyone has to log in again once.

**Tests and checks:**
```bash
pip install -r requirements-dev.txt
pytest -q
ruff check --select E,F,W,B --ignore E501,E702,E741,B008,B904 app scripts tests
bandit -q -r app scripts -s B608
pip-audit -r requirements.txt
```

## Loading CFC's data

1. **Stock history** — Setup → Import / export → *Import stock history*: choose `oct 2026 stock movement.xlsx`, *Analyse*, confirm the unit of every item (the system suggests but never guesses), review the discrepancy list, import. Each sheet becomes a closed read-only day; the last closing becomes the next opening. The workbook file is never modified. Findings for the October workbook: [docs/WORKBOOK_RECONCILIATION.md](docs/WORKBOOK_RECONCILIATION.md).
2. **Costs & thresholds** — Setup → Items & thresholds: set purchase cost, reorder level (and optionally critical level) for each item. Until costs are set, valuation and gross profit show warnings.
3. **Menu & recipes** — Setup → Import / export → *Download Excel*, fill *Recipes* (one row per ingredient per dish, raw quantity per plate), *Check file*, then *Import*.

## Backups

Run `python scripts/eod_reports.py` after closing to file the end-of-day report pack in `data/reports/`, and `python scripts/backup.py` nightly (Task Scheduler). It makes a verified copy in `data/backups/` and keeps 30. Copy that folder off the machine regularly. Back up `data/secret.key` **separately** (`backup.py --key`). Restore steps are in `scripts/backup.py`.

## Known limitations / roadmap

- **Offline:** the LAN server must be reachable to sell (internet is not needed). If a till loses the LAN mid-payment it can retry safely, but there is no offline sales queue. See docs/DEPLOYMENT.md.
- **Payments are confirmed by the cashier**, not verified: M-Pesa codes are format-checked and can't be reused; there is no Daraja (STK push / C2B callback) integration yet. Card payments rely on the terminal's approval slip.
- **KRA eTIMS** is not integrated; receipts state they are not fiscal receipts when tax is shown.
- Single store (no multi-branch or multi-tenant data isolation); a single cash drawer per cashier shift; no split payments yet.
- Batch/prep recipes (raw → cooked batch → portions), modifiers, kitchen tickets and direct ESC/POS printing are still on the roadmap.
- Receipt printing goes through the browser print dialog (Windows printer driver / Android print service).

---
© 2026 Elisha Gilbert. Private repository, all rights reserved.
