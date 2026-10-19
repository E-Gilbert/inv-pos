"""Emergency access: give an admin a new temporary PIN (must be changed at next login).

Needs physical/console access to the server, by design. Logged in the audit log.

    python scripts/reset_admin_pin.py              # resets the first active admin
    python scripts/reset_admin_pin.py "Jane"       # resets the admin with that name
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import audit, init_db, tx  # noqa: E402
from app.security import hash_pin, weak_pin  # noqa: E402

init_db()
name = sys.argv[1] if len(sys.argv) > 1 else None
while True:
    pin = f"{secrets.randbelow(900000) + 100000}"
    if not weak_pin(pin):
        break
with tx() as c:
    q = "SELECT id, name FROM staff WHERE role='admin'" + (" AND name = ?" if name else " AND active = 1") + " ORDER BY id LIMIT 1"
    row = c.execute(q, (name,) if name else ()).fetchone()
    if not row:
        sys.exit("No matching admin found.")
    if c.execute("SELECT 1 FROM staff WHERE pin_hash = ? AND id <> ?", (hash_pin(pin), row["id"])).fetchone():
        sys.exit("Generated PIN collided, run again.")
    c.execute("UPDATE staff SET pin_hash = ?, must_change_pin = 1, active = 1 WHERE id = ?", (hash_pin(pin), row["id"]))
    c.execute("DELETE FROM sessions WHERE staff_id = ?", (row["id"],))
    audit(c, None, "staff.pin_reset_console", "staff", row["id"], {"name": row["name"]})
print(f"Temporary PIN for {row['name']}: {pin}  (must be changed at first login)")
