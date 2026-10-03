"""Demo RBAC: roles, signed sessions, and the permission matrix.

This is a **demonstration** access-control layer, not a production identity
provider. It exists because the audit found that every write action - filing a
SAR, approving it, escalating to a regulator - was reachable from any browser on
the internet by any caller, with ``Access-Control-Allow-Origin: *`` on the way
in. For a compliance artefact that is the difference between a demonstration
and a liability.

What it provides
----------------
* three roles with explicit, reviewable powers;
* an HMAC-signed, expiring session token (no server-side session store, so it
  survives the stateless gunicorn workers used in production);
* a single permission matrix that the API consults before every action;
* a typed refusal reason, so the UI can explain a 403 instead of hiding it.

The signing secret comes from ``RISK_SESSION_SECRET``. When it is unset the
service generates a per-process random secret and marks every session
``ephemeral``, so a demo without configuration is still *safe* (tokens do not
survive a restart) rather than silently insecure (a shared hardcoded key).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional, Set, Tuple

#: Session lifetime. Short enough that a stale browser tab cannot hold a
#: meaningful authority window; long enough for a demo session.
SESSION_TTL_SECONDS = 8 * 60 * 60

#: The three demo roles, ordered least to most privileged.
ROLES = ("analyst", "approver", "admin")

#: Named users offered by the demo login selector. Real deployments would
#: resolve identity from the corporate directory instead.
DEMO_USERS = {
    "analyst": ("analyst@bank.com", "a.analyst"),
    "approver": ("approver@bank.com", "k.approver"),
    "admin": ("admin@bank.com", "s.admin"),
}

#: What each role may do. This matrix is the whole of the access model; the API
#: layer asks :func:`can` and nothing else decides permissions.
PERMISSIONS: Dict[str, Set[str]] = {
    "analyst": {
        "ask", "detect", "scan", "file", "escalate",
        "view_findings", "view_policy", "view_audit",
    },
    "approver": {
        "ask", "detect", "view_findings", "view_policy", "view_audit",
        "approve", "reject", "export",
    },
    "admin": {
        "ask", "detect", "scan", "file", "escalate", "approve", "reject",
        "view_findings", "view_policy", "view_audit", "export", "admin",
    },
}

#: API action -> required permission. Read-only actions appear here too so the
#: matrix is complete and auditable in one place rather than implied by the
#: absence of a check.
ACTION_PERMISSION: Dict[str, str] = {
    "health": "ask", "status": "ask", "examples": "ask", "ask": "ask",
    "detect": "detect", "scan": "scan", "findings": "view_findings",
    "policy": "view_policy", "file": "file", "approve": "approve",
    "escalate": "escalate", "board_pack": "export", "liquidity": "export",
    "audit": "view_audit", "corpus": "ask", "mcp": "ask",
    "evaluate": "view_findings", "automation": "ask", "login": "ask",
    "logout": "ask", "whoami": "ask",
}

_process_secret: Optional[bytes] = None


def _secret() -> Tuple[bytes, bool]:
    """Return (signing key, is_ephemeral)."""
    global _process_secret
    configured = os.environ.get("RISK_SESSION_SECRET", "").strip()
    if configured:
        return configured.encode("utf-8"), False
    if _process_secret is None:
        _process_secret = secrets.token_bytes(32)
    return _process_secret, True


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user: str, role: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    """Create a signed session token.

    Format ``<payload_b64>.<sig_b64>``. The signature covers the payload, so a
    client cannot promote itself from analyst to admin by editing the claims.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    if not user:
        raise ValueError("a session requires a named user")
    key, _ = _secret()
    payload = {"user": user, "role": role, "iat": int(time.time()),
               "exp": int(time.time()) + ttl}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (claims, error). Exactly one of the two is None.

    Checks, in order: well-formedness, signature, expiry, known role. Each
    failure returns a distinct reason so the UI can say what went wrong without
    leaking whether a forged token was merely expired or badly signed.
    """
    if not token or "." not in token:
        return None, "missing_or_malformed_token"
    body, _, sig = token.partition(".")
    key, _ = _secret()
    expected = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    # Constant-time compare: a timing oracle on the signature is a real
    # vulnerability, not a theoretical one.
    if not hmac.compare_digest(sig, expected):
        return None, "invalid_signature"
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None, "malformed_claims"
    if claims.get("role") not in ROLES:
        return None, "unknown_role"
    if int(claims.get("exp", 0)) < int(time.time()):
        return None, "session_expired"
    return claims, None


def can(role: Optional[str], permission: str) -> bool:
    return permission in PERMISSIONS.get(role or "", set())


def required_for(action: str) -> Optional[str]:
    """Permission required by an API action, or None when the action is open."""
    return ACTION_PERMISSION.get(action)


def authorize(action: str, claims: Optional[Dict[str, Any]]) -> Tuple[bool, Dict[str, Any]]:
    """Check one action for a session. Returns (allowed, detail).

    An unrecognised action is refused rather than allowed by default. Failing
    closed is the only safe default for a permission check.
    """
    needed = required_for(action)
    if needed is None:
        return False, {"error": "unknown_action", "action": action,
                       "reason": f"No permission is defined for action {action!r}."}
    if claims is None:
        return False, {"error": "authentication_required", "action": action,
                       "required_role": _roles_with(needed),
                       "reason": "This action requires a signed-in session. "
                                 "POST {\"action\":\"login\",\"role\":\"analyst\"} "
                                 "to obtain one."}
    role = claims.get("role")
    if not can(role, needed):
        return False, {"error": "insufficient_role", "action": action,
                       "role": role, "required_permission": needed,
                       "required_role": _roles_with(needed),
                       "reason": f"Role {role!r} may not perform {action!r}; "
                                 f"it requires {needed!r}."}
    return True, {"role": role, "user": claims.get("user")}


def _roles_with(permission: str) -> list:
    return [r for r in ROLES if permission in PERMISSIONS[r]]


def cookie_header(token: str, max_age: int = SESSION_TTL_SECONDS) -> str:
    """Session cookie. HttpOnly so page scripts cannot read the token, and
    SameSite=Lax so it is not attached to cross-site POSTs."""
    return (f"risk_session={token}; Path=/; Max-Age={max_age}; "
            f"HttpOnly; SameSite=Lax")


def clear_cookie_header() -> str:
    return "risk_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


def parse_cookies(header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            cookies[key.strip()] = value.strip()
    return cookies


def auth_status(claims: Optional[Dict[str, Any]], error: Optional[str] = None) -> Dict[str, Any]:
    """Describes the current session for the UI, including the role matrix.

    Shipping the matrix to the client lets the console disable buttons a role
    cannot use, instead of offering an action and then returning a 403.
    """
    _, ephemeral = _secret()
    return {
        "authenticated": claims is not None,
        "user": (claims or {}).get("user"),
        "role": (claims or {}).get("role"),
        "error": error,
        "ephemeral_secret": ephemeral,
        "roles": list(ROLES),
        "permissions": {r: sorted(p) for r, p in PERMISSIONS.items()},
        "demo_users": {r: {"email": u[0], "short": u[1]} for r, u in DEMO_USERS.items()},
    }
