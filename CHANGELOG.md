# Changelog

## 1.1.0-dev — 2026-11-04 (records import, analytics, CFC branding)

- **Past records in any common layout.** Setup → Import, and the first *Open day* screen, read CFC's workbook (one sheet per day) or a flat table (CSV, or one Excel sheet with a date column). Column names are recognised from common variants (received / purchases / delivered, closing / count, sales / sold, waste, unit, unit cost, remarks…); dates in 03/11/2026, 2026-11-03, 3 Nov 2026 or real Excel dates; comma, semicolon or tab separated. A blank opening is taken from the item's previous closing.
- **Units without typing.** With `auto_units`, each item's unit comes from the file's unit column, the matching stock item, or the numbers and name; only items that can't be told apart are asked about. Unit costs in the file set the cost of items that have none.
- **Import and open today.** `open_day=true` opens today straight after the import with the last closing carried over.
- Data findings in imported files (carry-over breaks, formula mismatches, closing above opening + received) are now warnings that are imported as recorded; only structural problems (unreadable dates, text in number cells, duplicate rows, missing closing) block an import.
- **Automatic day opening** (Settings → Opening the day, or `INVPOS_AUTO_OPEN_DAY=1`): the first stock entry of a new day opens it with last night's closing. Closing still needs the count.
- An item with no purchase cost takes the first price it is received at, under every costing method.
- **Analytics** (`GET /api/analytics?days=7|30|90`): stock used / received / wasted / short per day, waste by reason, biggest shortages, most used, purchases by supplier, value by category, days of cover, and short findings. Values only for roles that see costs.
- **Dashboard** rebuilt on it: period switch, KPIs, daily chart, findings, stock health, runs-out-soonest, shortage / waste / usage / supplier breakdowns. No external libraries.
- **City Food Court look**: charcoal and gold theme, Playfair Display and Lato served from the app (no internet needed), CFC logo on the PIN screen, header and app icons. Receipts still print black on white.
- 7 new tests (71 total).

## 1.1.0-dev — 2026-11-04 (cloud mode)

INV-POS can now also run in the cloud as the inventory engine behind the CFC website's /dashboard, while MICROS keeps selling. The LAN install is unchanged.

- **Postgres backend** (`DATABASE_URL=postgresql://...`, e.g. Supabase): same queries, translated by `app/pg.py`; schema in `app/schema_pg.sql`, created on first start in its own schema (`INVPOS_PG_SCHEMA`, default `inventory`). Writes are serialised with an advisory lock, giving the same guarantees as SQLite's BEGIN IMMEDIATE. Connection pool, Nairobi time zone per connection.
- **Inventory-only mode** (`POS_MODULE=off`): till, open checks and cash shifts are switched off (API returns 404, UI hides them); stock, purchasing, days, reports, imports and roles work as before.
- **Website logins**: with `SUPABASE_URL` set, `Authorization: Bearer <Supabase access token>` is accepted. Signature (JWKS or legacy secret), issuer, audience and expiry are checked, then the e-mail is matched to an active staff member, whose INV-POS role applies. Staff get an optional *Website login e-mail* (Setup → Staff, unique). Schema v5 adds it on SQLite too.
- **CORS** for the website origins (`CORS_ORIGINS`).
- Cloud hosts have no lasting disk: the PIN pepper comes from `INVPOS_SECRET` and the licence server id from `INVPOS_SERVER_ID` (cloud mode refuses to start without the secret).
- `render.yaml` blueprint, `requirements-cloud.txt`, docs/CLOUD.md (Supabase, Render, domain, linking staff, Next.js fetch helper, backups).
- Two report queries fixed to be valid standard SQL (grouping by name). The results are the same on SQLite.
- 8 new tests (63 total), and the whole suite also passes on Postgres.

## 1.0.0-rc.1 — 2026-10-29

Release candidate for installation at City Food Court.

**Menu**
- CFC's full printed menu (`CFC_Menu_Food_and_Drinks.pdf`) as `menus/cfc_menu.csv`: 328 items in 25 categories, 325 on sale, 3 hidden until priced (the only items left for review); where the PDF disagreed with itself the lower price is used. See docs/CFC_MENU.md.
- Menu CSV import/export (Setup → Import / export and `scripts/load_menu.py`): all-or-nothing, preview, price changes audited, never deletes, optional hide-missing.
- Option groups (sizes/choices) shown as one tile with a picker; descriptions; sale days (Thursday pizza special enforced on the server); zero-price included accompaniments; per-line notes.
- `--cfc` profile: VAT 16 % **included** in menu prices (total = menu price, VAT shown inside it), business details, order types incl. Glovo / Uber Eats, resale items linked to stock.

**Open checks (MICROS guest checks)**
- Servers open a check per table or name, add items over time and *Send* them (kitchen ticket prints); the cashier takes payment later. Pro-forma bill, reprint, hold a counter sale as a check.
- Removing a sent item or cancelling a check with sent items needs a manager (or manager PIN) and a reason; shown in the Voids report and audit log. The day can't close while checks are open.
- Employee sales are credited to the server who opened the check; the guest check journal shows table and server.

**Install & polish**
- `setup.bat` checks Python, builds the venv and offers to load the CFC menu; `scripts/windows/install_tasks.bat` registers server autostart, nightly backup, end-of-day reports and the firewall rule. See docs/INSTALL_WINDOWS.md.
- Replaces the MICROS "reset": a watchdog task restarts the server automatically if it freezes or stops (checked every 2 minutes), `restart_server.bat` restarts it on demand, and a reloaded till keeps its unsent cart. Plus `reset_admin_pin.bat`.
- First-run setup checklist on the dashboard; business phone on receipts; 2-decimal money where needed; tablet layout fixes.
- Schema v4 migration (automatic backup first). 10 new tests (55 total).

## 0.3.0 — 2026-10-26

Matches the core report set of CFC's current MICROS RES 3700 back office (see docs/MICROS_PARITY.md).
- New reports engine (`app/reports.py`): sales detail with tender media, order types and daily lines; tax; time period (hourly); serving period; cashier shifts over/short; guest check journal; employee sales by order type; major group; category (family group); menu item sales with food cost and margin; discount, void and refund reason reports. Daily or any date range; on-screen, print, CSV.
- Reports hub laid out like the MICROS menu, with quick date ranges and a one-click end-of-day pack; `scripts/eod_reports.py` for scheduled filing (the "autosequence").
- Checkout records order type (configurable list) and an optional guest count; receipts show them.
- Categories get a major group (Setup → Categories). Settings for order types, guest prompt and serving periods (validated: no overlaps).
- Schema v3 migration (backed up automatically). 6 new tests with hand-checked figures (45 total).

## 0.2.0 — 2026-10-19

**Data safety & security**
- Versioned migrations with automatic pre-migration backup; v0.1 databases upgrade in place.
- PINs hashed (HMAC-SHA256 + server secret); sessions hashed, expiring; login throttling; forced change of default/weak manager/admin PINs; at least one admin always kept.
- BEGIN IMMEDIATE transactions (fixes duplicate receipt-number errors with concurrent tills); idempotency keys on checkout, refunds, receiving and stock movements.
- Server-side permission table with a new **stock controller** role; audit log of sensitive actions; security headers + CSP; upload/body limits; generic error responses.
- Recipe import can no longer silently change the unit of an item with stock history; duplicate rows are rejected; *Check file* preview.
- Dependencies upgraded to versions with no known vulnerabilities; correct CI (pytest, ruff, bandit, pip-audit on Windows + Linux) replaces the non-working Django workflow.

**Inventory**
- Stock status colours (red/amber/green/blue/grey) with labels, configurable reorder and critical levels, dashboard alerts, filters, usage-based reorder suggestions.
- Suppliers, purchase orders, partial deliveries, close-short; stock rises only when goods are received.
- Manager stock adjustments; SKU, barcode, category, main supplier per item; costing methods (manual / last purchase / weighted average); cost snapshot on every sale for accurate COGS.
- Daily stock-movement workbook importer with discrepancy analysis and confirmed units.

**POS**
- Barcode/SKU/name search, Windows keyboard shortcuts, discounts with manager-PIN approval, configurable tax (inclusive/exclusive), duplicate M-Pesa code protection, safe retry after network loss.
- Cash shifts (float, expected vs counted cash), refunds (partial/full, restock option, manager approval), void rules.
- Reports: COGS, gross profit and margin, refunds, discounts, tax, purchases, waste, stock differences, valuation; CSV exports.

**Platform**
- Responsive Android layout (touch sizes, order bar), installable web app manifest, connection-lost banner.
- Licensing foundation (signed offline licences, expiry/grace/renewal/revocation/device limits) in **development mode** by default.
