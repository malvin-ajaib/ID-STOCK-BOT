"""Authentication: log in fresh every run (no session is persisted).

Each boot runs login -> PIN validate to obtain an access token. The refresh
token is kept in memory on the client so a 401 can refresh once and retry.
"""
from __future__ import annotations

from typing import Any, Optional

import config
from src.client import ApiClient, ApiError


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
    """Exchange the in-memory refresh token for a new access token.

    Authenticates with the refresh token (``jwt`` scheme) plus the ``User-Id``
    header and applies the new token to the client. Returns the new access
    token, or None if refresh isn't possible.
    """
    rt = client.refresh_jwt
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

    new_refresh = _extract_refresh(response)
    if new_refresh:
        client.refresh_jwt = new_refresh
    client.set_token(new_access)
    return new_access


def authenticate(client: ApiClient, account: dict) -> dict:
    """Run the full login -> PIN validate flow and apply the token in memory."""
    pin_token = login(client, account)
    validate_response = validate_pin(client, account, pin_token)

    # Prefer a fresh access token returned by the validate step; fall back to
    # the pin_token if validation does not return a new one.
    access_token = _extract_token(validate_response) or pin_token

    client.set_token(access_token)
    client.refresh_jwt = _extract_refresh(validate_response)

    ajaib_id = fetch_ajaib_id(client)

    return {
        "token": access_token,
        "pin_token": pin_token,
        "refresh_token": client.refresh_jwt,
        "ajaib_id": ajaib_id,
        "raw_validate": validate_response,
    }


def ensure_authenticated(client: ApiClient, account: dict, force: bool = False) -> dict:
    """Log in fresh on every call (no session is cached)."""
    return authenticate(client, account)
