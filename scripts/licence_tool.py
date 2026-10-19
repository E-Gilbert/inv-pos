"""Vendor tool: create the production signing key, issue licences and revocation lists.

Keep the PRIVATE key off customer machines and out of git (e.g. a password manager / USB key).

    python scripts/licence_tool.py genkey                      # prints a new private + public key
    python scripts/licence_tool.py issue --key PRIVATE --customer "City Food Court" \
           --expires 2027-10-31 --max-devices 3 [--server-id abcd1234] [--plan standard] [--grace 14]
    python scripts/licence_tool.py revoke --key PRIVATE LID [LID ...]

Install the PUBLIC key on the customer's server as data/licence_public.key before enforcing.
For development/testing use the key in tests/dev_licence_key.txt (rejected in enforce mode).
"""
import argparse
import base64
import os
import secrets
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.licence import sign  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("genkey")
    i = sub.add_parser("issue")
    i.add_argument("--key", required=True)
    i.add_argument("--customer", required=True)
    i.add_argument("--expires", required=True)
    i.add_argument("--max-devices", type=int, default=0)
    i.add_argument("--server-id")
    i.add_argument("--plan", default="standard")
    i.add_argument("--grace", type=int, default=14)
    r = sub.add_parser("revoke")
    r.add_argument("--key", required=True)
    r.add_argument("lids", nargs="+")
    a = p.parse_args()
    if a.cmd == "genkey":
        from cryptography.hazmat.primitives import serialization as s
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.generate()
        print("PRIVATE (keep secret):", base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode())
        print("PUBLIC  (data/licence_public.key):", base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode())
    elif a.cmd == "issue":
        date.fromisoformat(a.expires)
        payload = {"lid": "L-" + secrets.token_hex(6), "customer": a.customer, "plan": a.plan,
                   "issued": date.today().isoformat(), "expires": a.expires, "grace_days": a.grace,
                   "max_devices": a.max_devices}
        if a.server_id:
            payload["server_id"] = a.server_id
        print(sign(payload, a.key))
    else:
        print(sign({"revoked": a.lids, "issued": date.today().isoformat()}, a.key))


if __name__ == "__main__":
    main()
