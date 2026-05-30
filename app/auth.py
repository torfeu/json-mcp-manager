"""
Optional Bearer-token auth for the MCP Manager API.

Configuration (environment variables, evaluated at startup):
  MCP_MANAGER_PASSWORD       Plain-text password — hashed with SHA-256 at startup.
  MCP_MANAGER_PASSWORD_HASH  Pre-hashed SHA-256 hex digest (takes precedence).

If neither variable is set:
  - Binding to 127.0.0.1  → auth disabled, access unrestricted (local-only).
  - Binding to 0.0.0.0    → startup warning; access still allowed but strongly
                             discouraged without a password.
"""
import hashlib
import os
from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_password_hash: Optional[str] = None   # SHA-256 hex digest, or None = no auth

_bearer = HTTPBearer(auto_error=False)


def configure_auth() -> bool:
    """Read env vars and set up the password hash. Returns True if auth is active."""
    global _password_hash
    raw_hash = os.environ.get("MCP_MANAGER_PASSWORD_HASH", "").strip()
    raw_pw   = os.environ.get("MCP_MANAGER_PASSWORD", "").strip()

    if raw_hash:
        _password_hash = raw_hash.lower()
        return True
    if raw_pw:
        _password_hash = hashlib.sha256(raw_pw.encode()).hexdigest()
        return True

    # Fall back to persisted settings (runtime/settings.json)
    try:
        from .settings_store import load_settings
        stored_hash = load_settings().get("password_hash")
        if stored_hash:
            _password_hash = stored_hash
            return True
    except Exception:
        pass

    _password_hash = None
    return False


def verify_password(plain: str) -> bool:
    """Return True if *plain* matches the active password hash."""
    if _password_hash is None:
        return True  # no auth set — nothing to verify
    return hashlib.sha256(plain.encode()).hexdigest() == _password_hash


def set_password(plain: str) -> None:
    """Update the active password hash in memory and persist it to the settings file."""
    global _password_hash
    from .settings_store import save_settings
    new_hash = hashlib.sha256(plain.encode()).hexdigest() if plain else None
    _password_hash = new_hash
    save_settings({"password_hash": new_hash})


def set_edit_mode_setting(mode: str) -> None:
    """Update edit mode at runtime and persist to the settings file."""
    from .settings_store import save_settings
    if mode == "full":
        os.environ.pop("MCP_EDIT_MODE", None)
        save_settings({"edit_mode": None})
    else:
        os.environ["MCP_EDIT_MODE"] = mode
        save_settings({"edit_mode": mode})


# ── MCP Bearer Token ──────────────────────────────────────────────────────────

def mcp_bearer_token() -> Optional[str]:
    """Return the active MCP Bearer token, or None if MCP auth is disabled."""
    return os.environ.get("MCP_BEARER_TOKEN") or None


def token_edit_enabled() -> bool:
    """Return False when --no-token-edit was passed at startup."""
    return os.environ.get("MCP_NO_TOKEN_EDIT") != "1"


def set_mcp_bearer_token(token: Optional[str]) -> None:
    """Update the MCP Bearer token at runtime and persist it."""
    from .settings_store import save_settings
    if token:
        os.environ["MCP_BEARER_TOKEN"] = token
    else:
        os.environ.pop("MCP_BEARER_TOKEN", None)
    save_settings({"mcp_bearer_token": token})


def auth_enabled() -> bool:
    return _password_hash is not None


def edit_mode() -> str:
    """Returns 'full' (default), 'upload' (no code editing), or 'readonly' (no mutations)."""
    return os.environ.get("MCP_EDIT_MODE", "full")


def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    """FastAPI dependency — raises 401 when auth is active and token is wrong."""
    if _password_hash is None:
        return  # auth disabled

    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if hashlib.sha256(token.encode()).hexdigest() != _password_hash:
        raise HTTPException(
            status_code=401,
            detail="Invalid password",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Auto-configure at import time so auth is active even when admin_server is
# imported directly (e.g. uvicorn app.admin_server:app) without going through manager.py.
configure_auth()
