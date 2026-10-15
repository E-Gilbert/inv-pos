"""Make a safe copy of the live database into data/backups/ (keeps the last 30).

Run nightly after closing (Windows Task Scheduler / cron), and copy the backups
folder to a flash drive or cloud now and then. A dead disk must not lose the books.
"""
import glob
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import DB_PATH  # noqa: E402

out_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
os.makedirs(out_dir, exist_ok=True)
target = os.path.join(out_dir, f"cfc-{datetime.now():%Y%m%d-%H%M%S}.db")
src = sqlite3.connect(DB_PATH)
dst = sqlite3.connect(target)
src.backup(dst)  # consistent copy even while the POS is running
dst.close(); src.close()
for old in sorted(glob.glob(os.path.join(out_dir, "cfc-*.db")))[:-30]:
    os.remove(old)
print("Backup saved:", target)
