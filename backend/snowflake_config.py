"""Snowflake credential resolution from the environment.

Credentials are read from environment variables, optionally seeded by a gitignored
``.env`` file. Nothing here writes, logs or echoes a secret, and no secret is
committed: ``.gitignore`` covers ``.env*`` and only ``.env.example`` is tracked.

Two authentication methods are supported, matching the Snowflake connector:

* **Programmatic OAuth token** (``SNOWFLAKE_TOKEN``) - shortest lived, preferred.
* **Key-pair auth** - private key at ``SNOWFLAKE_PRIVATE_KEY_PATH`` (or PEM text in
  ``SNOWFLAKE_PRIVATE_KEY``) plus ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE``.

A password is accepted only for demo accounts and is flagged as such in
:func:`auth_summary` so the UI and the audit log can say which method is in use.

:func:`resolve` never raises. It returns ``None`` when the environment is not
configured, which is the signal the engine factory uses to select the sqlite
fallback. That keeps "no credentials" a normal, supported deployment rather than a
crash.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

#: Environment variables holding a secret. Values are never returned to callers for
#: display; only their presence is reported.
SECRET_VARS = (
    "SNOWFLAKE_TOKEN",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_PRIVATE_KEY",
    "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
)

#: Environment variables that are safe to show in the UI or write to an audit log.
IDENTIFYING_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_ROLE",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
)

_LOADED_ENV_FILE: Optional[str] = None


def load_dotenv(path: Optional[str] = None) -> Optional[str]:
    """Seed ``os.environ`` from a ``.env`` file. Existing variables win.

    Deliberately not a dependency: ``python-dotenv`` is not installed, and the
    parser below is enough for ``KEY=value`` lines. The file is only ever read.
    """
    global _LOADED_ENV_FILE
    path = path or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return None
    _LOADED_ENV_FILE = path
    return path


def env_file_path() -> Optional[str]:
    return _LOADED_ENV_FILE


def auth_method() -> Optional[str]:
    """Which authentication method is configured, or ``None`` if unconfigured."""
    if os.environ.get("SNOWFLAKE_TOKEN"):
        return "oauth_token"
    if (os.environ.get("SNOWFLAKE_PRIVATE_KEY") or
            os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")):
        return "key_pair"
    if os.environ.get("SNOWFLAKE_PASSWORD"):
        return "password"
    return None


def is_configured() -> bool:
    """True when an account, a user and a secret are all present.

    Mirrors exactly what :func:`connection_parameters` requires. If the two
    disagreed, the engine factory would select Snowflake and then fail to
    connect - so a partial configuration must report as *not* configured and
    fall back to sqlite cleanly.
    """
    load_dotenv()
    return (bool(os.environ.get("SNOWFLAKE_ACCOUNT"))
            and bool(os.environ.get("SNOWFLAKE_USER"))
            and auth_method() is not None)


def _private_key_pem() -> Optional[str]:
    """Private key as PEM text, from either the env var or the referenced file."""
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    if pem:
        return pem.replace("\\n", "\n")
    path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    return None


def connection_parameters() -> Optional[Dict[str, Any]]:
    """Connector keyword arguments, or ``None`` when not configured.

    Returns a fresh dict each call so a caller can mutate it (the Snowflake
    connector stores it) without leaking state between engine instances.
    """
    load_dotenv()
    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    user = os.environ.get("SNOWFLAKE_USER")
    method = auth_method()
    if not account or not method or not user:
        return None

    params: Dict[str, Any] = {
        "account": account,
        "user": user,
        "role": os.environ.get("SNOWFLAKE_ROLE", "RISK_COPILOT_ROLE"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "RISK_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "RISK_INTELLIGENCE"),
        # Keep the session predictable: a compliance system must not silently
        # inherit a caller's TIMESTAMP_MODE or CLIENT_PREFETCH_THREADS.
        "autocommit": False,
    }

    if method == "oauth_token":
        params["token"] = os.environ["SNOWFLAKE_TOKEN"]
    elif method == "key_pair":
        params["private_key"] = _private_key_pem()
        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        if passphrase:
            params["private_key_password"] = passphrase
    else:
        params["password"] = os.environ["SNOWFLAKE_PASSWORD"]

    # Private key is required for key_pair; without it the config is incomplete.
    if method == "key_pair" and not params.get("private_key"):
        return None
    return params


def _key_pair_file_present() -> bool:
    """Whether the referenced private key file exists on disk."""
    path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH") or ""
    return bool(path) and os.path.isfile(path)


def auth_summary() -> Dict[str, Any]:
    """Safe-to-display description of the credential state. No secret values."""
    load_dotenv()
    method = auth_method()
    summary: Dict[str, Any] = {
        "configured": is_configured(),
        "auth_method": method,
        "account": os.environ.get("SNOWFLAKE_ACCOUNT"),
        "user": os.environ.get("SNOWFLAKE_USER"),
        "role": os.environ.get("SNOWFLAKE_ROLE", "RISK_COPILOT_ROLE"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "RISK_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "RISK_INTELLIGENCE"),
        "env_file_loaded": _LOADED_ENV_FILE is not None,
        # Report presence per secret var without revealing any value.
        "secrets_present": [v for v in SECRET_VARS if os.environ.get(v)],
        "key_pair_file_present": _key_pair_file_present(),
        "cortex": {
            "analyst": os.environ.get("SNOWFLAKE_CORTEX_ANALYST_MODEL", "claude-3-5-sonnet"),
            "complete": os.environ.get("SNOWFLAKE_CORTEX_COMPLETE_MODEL", "claude-3-5-sonnet"),
            "agent": os.environ.get("SNOWFLAKE_CORTEX_AGENT_NAME", "RISK_COMPLIANCE_AGENT"),
        },
    }
    return summary
