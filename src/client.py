"""Thin HTTP client wrapper around requests.Session.

Every request automatically carries the device/app identity headers and, once
authenticated, the bearer token. All API access in the bot goes through here so
headers and auth live in exactly one place.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

import requests

import config


def _to_curl(prepared: requests.PreparedRequest) -> str:
    """Render a sent request as a reproducible ``curl`` command."""
    parts = [f"curl --location --request {prepared.method} '{prepared.url}'"]
    for key, value in prepared.headers.items():
        parts.append(f"--header '{key}: {value}'")
    body = prepared.body
    if body is not None:
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        parts.append(f"--data-raw '{body}'")
    return " \\\n  ".join(parts)


def _log_curl(
    prepared: requests.PreparedRequest,
    status_code: Optional[int],
    response: Any = None,
) -> None:
    """Append the request (curl), its status, and the response body to api.logs."""
    stamp = datetime.now().isoformat(timespec="seconds")
    outcome = f"HTTP {status_code}" if status_code is not None else "no response"
    if isinstance(response, (dict, list)):
        body_text = json.dumps(response, ensure_ascii=False)
    else:
        body_text = "" if response is None else str(response)
    entry = (
        f"# {stamp}  ->  {outcome}\n"
        f"{_to_curl(prepared)}\n"
        f"# response: {body_text}\n\n"
    )
    try:
        with open(config.API_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError:
        # Logging must never break an API call.
        pass


class ApiError(RuntimeError):
    """Raised when the API returns a non-2xx response."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ApiClient:
    def __init__(self, base_url: str = config.BASE_URL, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(config.DEVICE_HEADERS)
        self._token: Optional[str] = None
        # Refresh token held in memory (no session is persisted to disk).
        self.refresh_jwt: Optional[str] = None
        # Ajaib user id (from /api/v3/users/me/), held in memory for later use.
        self.ajaib_id: Optional[str] = None

        # Called once on a 401 to obtain a fresh token; returns the new token
        # (and is expected to apply it via set_token) or None if refresh failed.
        self._refresh_handler: Optional[Callable[[], Optional[str]]] = None

    def set_refresh_handler(self, handler: Optional[Callable[[], Optional[str]]]) -> None:
        self._refresh_handler = handler

    # -- auth state ---------------------------------------------------------
    def set_token(self, token: Optional[str], scheme: str = "jwt") -> None:
        """Attach (or clear) the auth token used for authenticated calls.

        Ajaib uses the ``jwt`` Authorization scheme (e.g. ``Authorization: jwt
        <token>``), which is why that is the default rather than ``Bearer``.
        """
        self._token = token
        if token:
            self._session.headers["Authorization"] = f"{scheme} {token}"
        else:
            self._session.headers.pop("Authorization", None)

    @property
    def is_authenticated(self) -> bool:
        return self._token is not None

    # -- low level ----------------------------------------------------------
    def request(self, method: str, path: str, _allow_refresh: bool = True, **kwargs) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            # Log the attempt even when the request never completed.
            if exc.request is not None:
                _log_curl(exc.request, None)
            raise

        # Parse JSON when possible; fall back to raw text.
        try:
            body = resp.json()
        except ValueError:
            body = resp.text

        _log_curl(resp.request, resp.status_code, body)

        # Session expired: refresh the token once and retry the same request.
        if resp.status_code == 401 and _allow_refresh and self._refresh_handler:
            new_token = self._refresh_handler()
            if new_token:
                # Drop any stale per-request Authorization so the refreshed
                # session header is used on retry.
                if isinstance(kwargs.get("headers"), dict):
                    kwargs["headers"].pop("Authorization", None)
                return self.request(method, path, _allow_refresh=False, **kwargs)

        if not resp.ok:
            raise ApiError(
                f"{method} {url} -> HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=body,
            )
        return body

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)
