"""Commercial licensing foundation: offline-verifiable signed licences.

A licence is `base64url(JSON payload) + "." + base64url(Ed25519 signature)`, signed with a private
key that only the vendor holds (scripts/licence_tool.py). The app embeds only the PUBLIC key, so
licences can't be forged from anything in this repository or on the customer's machine.

Payload fields:
    lid          licence id (used for revocation)
    customer     business name
    plan         e.g. "standard"
    issued       YYYY-MM-DD
    expires      YYYY-MM-DD (last valid day)
    grace_days   days after expiry before restrictions apply (default 14)
    max_devices  tills/tablets allowed to sell (0 = unlimited)
    server_id    optional: bind to one server install (see data/server.id)
    features     optional list

MODES (environment variable CFC_LICENCE_MODE, deliberately NOT a setting in the UI):
    development  (default) - licence status is computed and shown, NEVER enforced.
    enforce                - production. Must not be enabled until the owner approves it.

Even when enforced, an expired/missing licence only blocks NEW sales, stock receipts and day
openings. Login, viewing, reports, exports, closing the day and closing shifts always keep
working, so a licensing problem can never lock a business out of its own records. Verification
is fully offline, so a vendor-server outage cannot block anything.
"""
import base64
import json
import os
import secrets
from datetime import date, timedelta

from .db import DATA_DIR, now

# TEST/DEVELOPMENT key. Its private half is in tests/dev_licence_key.txt and is public knowledge,
# so licences signed with it are refused when CFC_LICENCE_MODE=enforce.
DEV_PUBLIC_KEY = "DSbSVWlIhMhdh/jelHVeIa2iWFZW+1JvrQEi0pLnMoM="
PUBKEY_FILE = os.path.join(DATA_DIR, "licence_public.key")
SERVER_ID_FILE = os.path.join(DATA_DIR, "server.id")
BLOCKING = ("unlicensed", "invalid", "revoked", "expired", "wrong_server")


def mode():
    m = os.environ.get("CFC_LICENCE_MODE", "development").strip().lower()
    return "enforce" if m == "enforce" else "development"


def public_key_b64():
    env = os.environ.get("CFC_LICENCE_PUBKEY")
    if env:
        return env.strip()
    if os.path.exists(PUBKEY_FILE):
        with open(PUBKEY_FILE) as f:
            return f.read().strip()
    return DEV_PUBLIC_KEY


def server_id():
    if not os.path.exists(SERVER_ID_FILE):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SERVER_ID_FILE, "w") as f:
            f.write(secrets.token_hex(8))
    with open(SERVER_ID_FILE) as f:
        return f.read().strip()


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(token):
    """Returns (payload, None) or (None, error message). Never raises on bad input."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        body, sig = token.strip().split(".")
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64()))
        pub.verify(_b64d(sig), body.encode())
        payload = json.loads(_b64d(body))
    except InvalidSignature:
        return None, "The licence signature is not valid."
    except Exception:
        return None, "This is not a licence key."
    for k in ("lid", "customer", "expires"):
        if k not in payload:
            return None, f"Licence is missing '{k}'."
    try:
        date.fromisoformat(payload["expires"])
    except ValueError:
        return None, "Licence expiry date is not valid."
    if mode() == "enforce" and public_key_b64() == DEV_PUBLIC_KEY:
        return None, "Production mode needs the production public key (data/licence_public.key)."
    return payload, None


def status(c, today=None):
    today = today or date.today()
    row = c.execute("SELECT * FROM licence WHERE id = 1").fetchone()
    terminals = len(selling_terminals(c))
    base = {"mode": mode(), "enforced": mode() == "enforce", "server_id": server_id(), "terminals": terminals,
            "dev_key": public_key_b64() == DEV_PUBLIC_KEY}
    if not row:
        return base | {"state": "unlicensed", "message": "No licence installed."}
    payload, err = verify(row["token"])
    if err:
        return base | {"state": "invalid", "message": err}
    info = {k: payload.get(k) for k in ("lid", "customer", "plan", "issued", "expires", "max_devices", "features", "server_id")}
    info["licence_server_id"] = info.pop("server_id")
    if c.execute("SELECT 1 FROM licence_revocations WHERE licence_id = ?", (payload["lid"],)).fetchone():
        return base | info | {"state": "revoked", "message": "This licence has been revoked."}
    if info["licence_server_id"] and info["licence_server_id"] != base["server_id"]:
        return base | info | {"state": "wrong_server", "message": "This licence is for a different server."}
    exp = date.fromisoformat(payload["expires"])
    grace = int(payload.get("grace_days", 14))
    days_left = (exp - today).days
    out = base | info | {"days_left": days_left, "grace_days": grace}
    if days_left >= 0:
        st = {"state": "valid", "message": f"Valid until {exp.isoformat()}."}
    elif today <= exp + timedelta(days=grace):
        st = {"state": "grace", "message": f"Expired on {exp.isoformat()}. Renew within {(exp + timedelta(days=grace) - today).days + 1} day(s)."}
    else:
        st = {"state": "expired", "message": f"Expired on {exp.isoformat()}. Renew to continue selling."}
    md = int(payload.get("max_devices") or 0)
    out["device_limit_exceeded"] = bool(md and terminals > md)
    return out | st


def install(c, token, staff_id):
    from .db import audit
    payload, err = verify(token)
    if err:
        raise ValueError(err)
    if c.execute("SELECT 1 FROM licence_revocations WHERE licence_id = ?", (payload["lid"],)).fetchone():
        raise ValueError("This licence has been revoked.")
    c.execute("""INSERT INTO licence(id, token, installed_at, installed_by) VALUES (1, ?, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET token = excluded.token, installed_at = excluded.installed_at,
                 installed_by = excluded.installed_by""", (token.strip(), now(), staff_id))
    audit(c, staff_id, "licence.install", "licence", None, {"lid": payload["lid"], "expires": payload["expires"]})
    return payload


def apply_revocations(c, token, staff_id):
    """A revocation list is signed like a licence: payload {"revoked": [lid, ...], "issued": date}."""
    from .db import audit
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        body, sig = token.strip().split(".")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64())).verify(_b64d(sig), body.encode())
        payload = json.loads(_b64d(body))
        ids = [str(x) for x in payload["revoked"]]
    except Exception:
        raise ValueError("Not a valid signed revocation list.")
    for lid in ids:
        c.execute("INSERT OR IGNORE INTO licence_revocations(licence_id, revoked_at, list_issued) VALUES (?, ?, ?)",
                  (lid, now(), payload.get("issued")))
    audit(c, staff_id, "licence.revocations", "licence", None, {"count": len(ids)})
    return len(ids)


def gate(c, action, terminal_id=None):
    """Raise LicenceBlocked if this action must be refused. Only ever blocks in enforce mode,
    and only for actions that create new business (sell, receive, open a day)."""
    if mode() != "enforce":
        return
    st = status(c)
    if st["state"] in BLOCKING:
        raise LicenceBlocked(f"{st['message']} Records, reports and exports remain available.")
    md = int(st.get("max_devices") or 0)
    if action == "sell" and md:
        selling = selling_terminals(c)
        if (terminal_id or "") not in selling and len(selling) >= md:
            raise LicenceBlocked(f"This licence allows {md} selling device(s). Deactivate an old device in Setup → Licence.")


def selling_terminals(c):
    """Active devices that have rung up at least one sale (back-office PCs don't count)."""
    return {r["terminal_id"] for r in c.execute(
        """SELECT DISTINCT o.terminal_id FROM orders o JOIN terminals t ON t.id = o.terminal_id
           WHERE t.active = 1""")}


class LicenceBlocked(Exception):
    pass


def sign(payload, private_key_b64):
    """Vendor-side helper (used by scripts/licence_tool.py and tests)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(key.sign(body.encode())).decode().rstrip("=")
    return f"{body}.{sig}"
