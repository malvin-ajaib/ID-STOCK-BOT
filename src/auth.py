"""Authentication: login -> PIN validate, with a cached session per account.

Sessions are persisted to config.SESSION_FILE keyed by account email, so multiple
terminals running the same account reuse ONE session instead of each logging in
fresh (which could invalidate the others). A 401 refreshes the token and updates
the cache; pass force=True (--force-login) to ignore the cache and log in again.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import config
from src.client import ApiClient, ApiError


def _load_store() -> dict:
    """Load the whole session store (a dict keyed by account email)."""
    try:
        return json.loads(config.SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_entry(email: str, entry: dict) -> None:
    """Persist one account's session, atomically, preserving other accounts."""
    store = _load_store()
    store[email] = entry
    tmp = f"{config.SESSION_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2)
        os.replace(tmp, config.SESSION_FILE)  # atomic; safe for concurrent readers
    except OSError:
        pass  # caching must never break auth


def _load_entry(email: str) -> Optional[dict]:
    entry = _load_store().get(email)
    return entry if isinstance(entry, dict) and entry.get("token") else None


def _extract_token(login_response: Any) -> Optional[str]:
    """Extract the access token from the login response.

    The Ajaib login endpoint returns a PIN-stage token in ``pin_token`` (the JWT
    header has ``"typ": "PIN"``). We treat that as the primary token. Common
    OAuth-style field names are also probed as a fallback in case the API shape
    changes.
    """
    if not isinstance(login_response, dict):
        return None

    # Flatten one level of nesting (e.g. {"data": {...}}) for convenience.
    candidates = [login_response]
    for key in ("data", "result", "tokens", "token"):
        nested = login_response.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    token_keys = (
        "pin_token",     # Ajaib login (PIN-stage) — primary
        "access_token",
        "accessToken",
        "access",
        "token",
        "id_token",
        "jwt",
    )
    for obj in candidates:
        for key in token_keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_refresh(response: Any) -> Optional[str]:
    """Extract the refresh token, including from a nested ``result``/``data``."""
    if not isinstance(response, dict):
        return None

    candidates = [response]
    for key in ("result", "data", "tokens"):
        nested = response.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for obj in candidates:
        for key in ("refresh_token", "pin_refresh", "refreshToken", "refresh"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_id(response: Any) -> Optional[Any]:
    """Extract the ``id`` field from /users/me (top-level or nested)."""
    if not isinstance(response, dict):
        return None
    for obj in (response, response.get("result"), response.get("data")):
        if isinstance(obj, dict) and obj.get("id") is not None:
            return obj["id"]
    return None


def fetch_ajaib_id(client: ApiClient) -> Optional[Any]:
    """GET /api/v3/users/me/ and store the ``id`` on the client (best-effort)."""
    url = f"{config.BASE_URL}{config.USERS_ME_PATH}"
    try:
        response = client.get(url)
    except ApiError:
        return None
    client.ajaib_id = _extract_id(response)
    return client.ajaib_id


def login(client: ApiClient, account: dict) -> str:
    """Step 1 — log in and return the PIN-stage token (``pin_token``)."""
    payload = {
        "email": account["email"],
        "platform": config.PLATFORM,
        "password": account["password"],
    }
    response = client.post(f"{config.BASE_URL}{config.LOGIN_PATH}", json=payload)

    pin_token = _extract_token(response)
    if not pin_token:
        raise RuntimeError(
            "Login succeeded but no pin_token was found. Inspect the response "
            "and adjust _extract_token() in src/auth.py.\n"
            f"  response: {response}"
        )
    return pin_token


def validate_pin(client: ApiClient, account: dict, pin_token: str) -> Any:
    """Step 2 — exchange the pin_token + PIN for the real session token.

    The validate endpoint authenticates with the PIN-stage token using the
    ``jwt`` scheme and also requires a ``User-Id`` header.
    """
    headers = {
        "Authorization": f"jwt {pin_token}",
        "User-Id": str(account["user_id"]),
    }
    payload = {"pin_code": account["pin_code"]}
    url = f"{config.BASE_URL}{config.PIN_VALIDATE_PATH}"
    return client.post(url, json=payload, headers=headers)


def refresh_token(client: ApiClient, account: dict) -> Optional[str]:
    """Refresh the access token, coordinating via the shared session store.

    If another process already refreshed (the cached token differs from ours),
    adopt that token instead of refreshing again. Otherwise exchange the refresh
    token for a new access token and update the cache. Returns the new token or
    None if refresh isn't possible.
    """
    email = account["email"]

    # Another terminal may have already refreshed — reuse its token if so.
    cached = _load_entry(email)
    if cached and cached.get("token") and cached["token"] != client._token:
        client.set_token(cached["token"])
        client.refresh_jwt = cached.get("refresh_token")
        return cached["token"]

    rt = client.refresh_jwt or (cached or {}).get("refresh_token")
    if not rt:
        return None

    headers = {
        "Authorization": f"jwt {rt}",
        "User-Id": str(account["user_id"]),
    }
    url = f"{config.BASE_URL}{config.REFRESH_PATH}"
    try:
        # _allow_refresh=False: never let a refresh trigger another refresh.
        response = client.request("POST", url, _allow_refresh=False, headers=headers)
    except ApiError:
        return None

    new_access = _extract_token(response)
    if not new_access:
        return None

    new_refresh = _extract_refresh(response) or rt
    client.refresh_jwt = new_refresh
    client.set_token(new_access)
    _save_entry(email, {
        "token": new_access,
        "refresh_token": new_refresh,
        "ajaib_id": client.ajaib_id,
        "refreshed_at": int(time.time()),
    })
    return new_access


def authenticate(client: ApiClient, account: dict) -> dict:
    """Run the full login -> PIN validate flow and cache the session."""
    pin_token = login(client, account)
    validate_response = validate_pin(client, account, pin_token)

    # Prefer a fresh access token returned by the validate step; fall back to
    # the pin_token if validation does not return a new one.
    access_token = _extract_token(validate_response) or pin_token

    client.set_token(access_token)
    client.refresh_jwt = _extract_refresh(validate_response)
    ajaib_id = fetch_ajaib_id(client)

    session = {
        "token": access_token,
        "refresh_token": client.refresh_jwt,
        "ajaib_id": ajaib_id,
        "obtained_at": int(time.time()),
    }
    _save_entry(account["email"], session)
    return session


def ensure_authenticated(client: ApiClient, account: dict, force: bool = False) -> dict:
    """Reuse this account's cached session if present; otherwise log in fresh.

    ``force=True`` (--force-login) ignores the cache and re-authenticates.
    """
    if not force:
        cached = _load_entry(account["email"])
        if cached:
            client.set_token(cached["token"])
            client.refresh_jwt = cached.get("refresh_token")
            client.ajaib_id = cached.get("ajaib_id")
            return cached
    return authenticate(client, account)
