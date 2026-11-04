"""Website logins (cloud mode): accept a Supabase access token instead of a till PIN.

The restaurant website's /dashboard already signs people in with Supabase. When it calls this
API it sends that login as `Authorization: Bearer <access token>`. We check the token's
signature, issuer, audience and expiry, then look up the *staff member with the same e-mail*.
Roles and permissions still come from INV-POS (Setup → Staff), never from the website, so a
website account with no matching active staff record gets nothing.

Keep "Confirm email" switched ON in Supabase (Authentication → Sign In / Providers), so nobody
can create an unconfirmed account using a staff member's address. If the website's dashboard
is staff-only, also switch off public sign-ups.

Configuration (environment):
    SUPABASE_URL          https://<project>.supabase.co   (turns this feature on)
    SUPABASE_JWT_SECRET   only for projects still on the legacy shared HS256 secret

New Supabase projects sign with asymmetric keys (ES256/RS256); those are fetched from the
project's public JWKS endpoint and cached, so no secret is needed for them.
"""
import os
import threading

import jwt

ALLOWED_ALGS = ("ES256", "RS256", "HS256")
_lock = threading.Lock()
_jwks_client = None
_jwks_url = None


class WebAuthError(Exception):
    pass


def supabase_url():
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def enabled():
    return bool(supabase_url())


def _signing_key(token):
    """Key for this token: the HS256 secret, or the matching public key from the project's JWKS."""
    global _jwks_client, _jwks_url
    alg = jwt.get_unverified_header(token).get("alg")
    if alg not in ALLOWED_ALGS:
        raise WebAuthError("Unsupported token algorithm.")
    if alg == "HS256":
        secret = os.environ.get("SUPABASE_JWT_SECRET", "")
        if not secret:
            raise WebAuthError("Token uses the legacy secret, but SUPABASE_JWT_SECRET is not set.")
        return secret, alg
    url = supabase_url() + "/auth/v1/.well-known/jwks.json"
    with _lock:
        if _jwks_client is None or _jwks_url != url:
            _jwks_client, _jwks_url = jwt.PyJWKClient(url, cache_keys=True, lifespan=600, timeout=10), url
        client = _jwks_client
    return client.get_signing_key_from_jwt(token).key, alg


def verify(token):
    """Returns the verified e-mail (lower-case) or raises WebAuthError."""
    if not enabled():
        raise WebAuthError("Website logins are not enabled on this server.")
    try:
        key, alg = _signing_key(token)
        claims = jwt.decode(token, key, algorithms=[alg], audience="authenticated",
                            issuer=supabase_url() + "/auth/v1", options={"require": ["exp", "sub", "aud", "iss"]},
                            leeway=30)
    except WebAuthError:
        raise
    except jwt.PyJWTError as e:
        raise WebAuthError(f"Website login not accepted ({e.__class__.__name__}).") from e
    if claims.get("is_anonymous") or claims.get("role") != "authenticated":
        raise WebAuthError("Website login not accepted (not a signed-in user).")
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise WebAuthError("Website login has no e-mail address.")
    return email
