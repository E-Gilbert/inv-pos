# Changelog

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
