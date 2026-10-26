# Security model and audit notes (v0.2)

## Authentication & sessions
* PIN login (4–6 digits). PINs are stored as HMAC-SHA256 with a random 256-bit server secret in `data/secret.key` (outside the database and out of git). A stolen database or backup alone does not reveal PINs.
* Throttling: 5 wrong PINs from one device lock it for 60 s, doubling up to 30 min; more than 40 failures in 10 min from all devices pauses logins for everyone for 60 s.
* Default admin PIN 1234 must be changed at first login. Managers and admins can't use repeated or sequential PINs. New or reset PINs must be changed by their owner.
* Sessions are random 256-bit tokens stored **hashed**, sent in an `X-Token` header (not a cookie, so CSRF doesn't apply), expiring after inactivity (default 12 h, at most 24 h). Changing a role, PIN or disabling a person ends their sessions.

## Authorisation
Every endpoint declares a permission (`app/security.py → PERMISSIONS`) that is checked on the server. Tests cover cashier/stock controller/manager/admin access, including attempts on forbidden endpoints. Cost data is stripped from responses for cashiers. There must always be one active admin.

## Data integrity
* Every request runs in one `BEGIN IMMEDIATE` SQLite transaction: a sale, its receipt number, its line items and its stock movements commit together or not at all. Concurrent tills queue (tested with 25 parallel sales).
* Idempotency keys on checkout, refunds, receiving and stock movements stop double submissions.
* Prices and totals come from the database, never the till. M-Pesa codes can't be reused on two paid orders.
* Stock balances are always recomputed from the movement log; nothing is overwritten.
* Schema migrations are versioned and back up the database first; foreign keys are checked after each migration.

## Web hardening
Content-Security-Policy (`script-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store` on API responses. All server text is HTML-escaped in the UI. Requests over 6 MB and uploads over 5 MB are refused. CSV exports neutralise spreadsheet formulas. All SQL uses parameters (bandit B608 hits were reviewed: only placeholder lists and fixed column names are interpolated). Unexpected errors return a reference code, never a stack trace; details go to `data/errors.log`.

## Audit log (Setup → Audit log, admin)
Logins and failed logins, PIN changes/resets, staff create/edit (role, status), settings changes (old → new), menu price and recipe changes, ingredient cost changes (manual and automatic), discounts and who approved them, voids, refunds, stock adjustments, purchase orders, imports, exports, day open/close, shift close with cash variance, licence and device changes.

## Payments
No payment is "verified" by the system: the cashier confirms cash, the M-Pesa message or the card slip. This is shown on screen. Daraja/STK push integration needs Safaricom credentials and must verify callbacks server-side before an order is marked paid.

## Checks run (v0.3)
* `pytest`: 45 tests, Python 3.10 and 3.13.
* `ruff` (E, F, W, B): clean. `bandit`: clean apart from the reviewed B608.
* `pip-audit -r requirements.txt`: no known vulnerabilities (dependencies upgraded: FastAPI 0.145, Starlette 1.3.1, python-multipart 0.0.31, cryptography 50.0.1).
* Browser end-to-end runs (Chromium, desktop 1366×800 and Android-size 412×915 touch): login, forced PIN change, open day, shift, scan/search, discount, cash and M-Pesa sales, refund with restock, waste, purchase order with partial receipt, reports, workbook analysis and import. No console errors.

## Known gaps
* HTTP on the LAN (see DEPLOYMENT.md for HTTPS).
* PIN-only login is fast but weaker than username + password; the throttle is the main protection.
* No multi-tenant isolation (single store by design).
* Backups are not encrypted; keep them on a device you control.
