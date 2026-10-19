"""Make a safe, verified copy of the live database into data/backups/ (keeps the last 30).

Run nightly after closing (Windows Task Scheduler / cron), and copy the backups folder to
a flash drive or cloud regularly. A dead disk must not lose the books.

    python scripts/backup.py            # database copy, checked with PRAGMA integrity_check
    python scripts/backup.py --key      # ALSO copies data/secret.key (store that copy separately!)

data/secret.key is needed to log in with the existing PINs. Without it, use
scripts/reset_admin_pin.py on the server to regain access (sales data is unaffected).

Restore: stop the server, copy a backup over data/cfc.db (delete cfc.db-wal / cfc.db-shm), start.
"""
import glob
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import DATA_DIR, DB_PATH  # noqa: E402

out_dir = os.path.join(DATA_DIR, "backups")
os.makedirs(out_dir, exist_ok=True)
stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
target = os.path.join(out_dir, f"cfc-{stamp}.db")
src = sqlite3.connect(DB_PATH)
dst = sqlite3.connect(target)
src.backup(dst)  # consistent copy even while the POS is running
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
src.close()
if ok != "ok":
    os.remove(target)
    sys.exit(f"Backup FAILED integrity check: {ok}")
for old in sorted(glob.glob(os.path.join(out_dir, "cfc-*.db")))[:-30]:
    os.remove(old)
if "--key" in sys.argv:
    key = os.path.join(DATA_DIR, "secret.key")
    if os.path.exists(key):
        shutil.copy2(key, os.path.join(out_dir, f"secret-{stamp}.key"))
print("Backup saved and verified:", target)
