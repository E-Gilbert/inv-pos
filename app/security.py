"""PIN hashing, session tokens, login throttling and the permission table.

PINs are short (4-6 digits), so a plain hash would be reversed instantly from a stolen
database or backup. They are stored as HMAC-SHA256 with a server secret ("pepper") that
lives in data/secret.key, outside the database. Keep that file out of git and back it up
separately from the database: without it, nobody can log in (see scripts/reset_admin_pin.py).

Because staff log in with the PIN alone (fast at a busy till), the hash must be deterministic
so it can be looked up; the pepper is what protects it. The real defence against guessing is
the login throttle below.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

from .db import DATA_DIR

SECRET_FILE = os.environ.get("CFC_SECRET_FILE", os.path.join(DATA_DIR, "secret.key"))
_secret = None


def secret():
    global _secret
    if _secret is None:
        if not os.path.exists(SECRET_FILE):
            os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
            fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        with open(SECRET_FILE) as f:
            _secret = bytes.fromhex(f.read().strip())
        if len(_secret) < 32:
            raise RuntimeError("data/secret.key is damaged (too short).")
    return _secret


def hash_pin(pin: str) -> str:
    return hmac.new(secret(), b"pin:" + pin.strip().encode(), hashlib.sha256).hexdigest()


def new_token():
    tok = secrets.token_urlsafe(32)
    return tok, hash_token(tok)


def hash_token(tok: str) -> str:
    return hashlib.sha256((tok or "").encode()).hexdigest()


def weak_pin(pin: str) -> bool:
    """Repeated digits (1111) or straight runs (1234, 4321) are guessed first."""
    p = pin.strip()
    if len(set(p)) == 1:
        return True
    d = [int(x) for x in p if x.isdigit()]
    if len(d) == len(p) and len(d) > 1:
        steps = {b - a for a, b in zip(d, d[1:])}
        if steps in ({1}, {-1}):
            return True
    return p in {"1212", "2580", "0852", "1122", "6969", "1004", "2000", "2026"}


def valid_pin_format(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 6


# ------------------------------------------------------------------ throttling

class LoginThrottle:
    """Per-client lockout plus a global brake.

    * 5 wrong PINs from one device -> locked 60 s, doubling each further lockout (max 30 min).
    * More than 40 wrong PINs in 10 minutes from all devices together -> everyone waits 60 s
      (stops an attacker who rotates IP addresses on the LAN).
    In-memory: correct for the single uvicorn process this app runs as.
    """
    PER_CLIENT = 5
    GLOBAL_LIMIT = 40
    GLOBAL_WINDOW = 600

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}       # key -> [fail_count, locked_until, lockouts]
        self.global_fails = []  # timestamps
        self.global_until = 0.0

    def check(self, key):
        now = time.time()
        with self.lock:
            if now < self.global_until:
                return int(self.global_until - now) + 1
            c = self.clients.get(key)
            if c and now < c[1]:
                return int(c[1] - now) + 1
        return 0

    def fail(self, key):
        now = time.time()
        with self.lock:
            c = self.clients.setdefault(key, [0, 0.0, 0])
            c[0] += 1
            if c[0] >= self.PER_CLIENT:
                c[2] += 1
                c[1] = now + min(60 * 2 ** (c[2] - 1), 1800)
                c[0] = 0
            self.global_fails = [t for t in self.global_fails if now - t < self.GLOBAL_WINDOW] + [now]
            if len(self.global_fails) > self.GLOBAL_LIMIT:
                self.global_until = now + 60
                self.global_fails = []

    def success(self, key):
        with self.lock:
            self.clients.pop(key, None)

    def reset(self):
        with self.lock:
            self.clients.clear()
            self.global_fails = []
            self.global_until = 0.0


throttle = LoginThrottle()


# ------------------------------------------------------------------ permissions
# Enforced on the server for every request. The UI only hides buttons for convenience.

ROLES = ("cashier", "stock_controller", "manager", "admin")
_ALL = set(ROLES)
_MGR = {"manager", "admin"}
_STOCK = {"stock_controller", "manager", "admin"}
_SELL = {"cashier", "manager", "admin"}

PERMISSIONS = {
    "pos.sell": _SELL,
    "pos.void": _MGR,
    "pos.refund": _MGR,
    "pos.discount_approve": _MGR,
    "shift.own": _SELL,
    "shift.manage": _MGR,
    "stock.view": _ALL,
    "stock.waste": _ALL,
    "stock.receive": _STOCK,
    "stock.adjust": _MGR,
    "purchasing": _STOCK,
    "day.manage": _MGR,
    "reports.view": _MGR,
    "catalog.edit": _MGR,
    "data.import": _MGR,
    "data.export": _MGR,
    "audit.view": {"admin"},
    "staff.manage": {"admin"},
    "settings.edit": {"admin"},
    "licence.manage": {"admin"},
}


def allowed(role, perm):
    return role in PERMISSIONS[perm]


def permissions_for(role):
    return sorted(p for p, roles in PERMISSIONS.items() if role in roles)
