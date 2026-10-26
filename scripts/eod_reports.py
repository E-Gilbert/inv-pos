"""End-of-day report pack (the equivalent of a MICROS autosequence).

Writes the standard closing reports for one business day to data/reports/<date>/:
one CSV per report plus eod-<date>.html (open and print). Read-only: it never changes data.

    python scripts/eod_reports.py                 # the most recently CLOSED business day
    python scripts/eod_reports.py 2026-10-20      # a specific day
    python scripts/eod_reports.py 2026-10-01 2026-10-31   # a date range (month-end pack)

Schedule it after closing time with Windows Task Scheduler, next to scripts/backup.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import reports  # noqa: E402
from app.db import DATA_DIR, init_db, tx  # noqa: E402

init_db()
with tx() as c:
    if len(sys.argv) > 1:
        d_from = sys.argv[1]
        d_to = sys.argv[2] if len(sys.argv) > 2 else d_from
    else:
        last = c.execute("SELECT business_date FROM days WHERE status='closed' ORDER BY business_date DESC LIMIT 1").fetchone()
        if not last:
            sys.exit("No closed business day yet.")
        d_from = d_to = last["business_date"]
    reps = [reports.run(c, k, d_from, d_to) for k in reports.EOD_PACK]
label = d_from if d_from == d_to else f"{d_from}_{d_to}"
out = os.path.join(DATA_DIR, "reports", label)
os.makedirs(out, exist_ok=True)
for r in reps:
    with open(os.path.join(out, f"{r['key']}.csv"), "wb") as f:
        f.write(reports.to_csv(r))
with open(os.path.join(out, f"eod-{label}.html"), "w", encoding="utf-8") as f:
    f.write(reports.to_html(reps, f"End-of-day reports {label}"))
print(f"{len(reps)} reports written to {out}")
