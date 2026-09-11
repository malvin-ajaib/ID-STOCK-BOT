"""Central configuration loaded from environment variables (.env).

All values have sensible defaults that mirror the working curl request, so the
bot runs out of the box once credentials are supplied.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load variables from a local .env file if present. Real environment variables
# always take precedence over .env values.
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Every API hit is appended here as a reproducible curl command.
API_LOG_FILE = BASE_DIR / "api.logs"

# Cached login sessions (per account), shared across concurrent runs so multiple
# terminals reuse one session instead of each logging in fresh.
SESSION_FILE = BASE_DIR / ".session.json"


def _get(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value if value is not None else default


# --- Device profile (IOS or ANDROID) --------------------------------------
# Single switch for the whole app. Flip AJAIB_DEVICE_PROFILE in .env.
DEVICE_PROFILE = _get("AJAIB_DEVICE_PROFILE", "IOS").strip().upper()
IS_ANDROID = DEVICE_PROFILE == "ANDROID"

# --- Accounts --------------------------------------------------------------
# Two accounts: BUY uses account 1, SELL uses account 2. The SELL account falls
# back to the BUY account when its AJAIB_SELL_* vars are unset (single-account).
ACCOUNT_BUY = {
    "email": _get("AJAIB_EMAIL", "malvin.rdn+10@ajaib.co.id"),
    "password": _get("AJAIB_PASSWORD", "Ajaib123!"),
    "pin_code": _get("AJAIB_PIN_CODE", "1234"),
    "user_id": _get("AJAIB_USER_ID", "1"),
}
# `or ACCOUNT_BUY[...]` so an unset *or* empty AJAIB_SELL_* falls back to acct 1.
ACCOUNT_SELL = {
    "email": _get("AJAIB_SELL_EMAIL", "") or ACCOUNT_BUY["email"],
    "password": _get("AJAIB_SELL_PASSWORD", "") or ACCOUNT_BUY["password"],
    "pin_code": _get("AJAIB_SELL_PIN_CODE", "") or ACCOUNT_BUY["pin_code"],
    "user_id": _get("AJAIB_SELL_USER_ID", "") or ACCOUNT_BUY["user_id"],
}

def account_for(side: str) -> dict:
    """Return the account credentials for a trade side ('buy' or 'sell')."""
    return ACCOUNT_SELL if side == "sell" else ACCOUNT_BUY

# Login payload "platform" follows the device profile unless explicitly set.
PLATFORM = _get("AJAIB_PLATFORM", "Android" if IS_ANDROID else "iOS")

# Base URL per device profile (login, PIN validate, orderbook, ...).
# Falls back to AJAIB_BASE_URL, then a sensible default.
_FALLBACK_BASE = _get("AJAIB_BASE_URL", "https://sm-ple.ajaib.tech")
if IS_ANDROID:
    BASE_URL = _get("AJAIB_ANDROID_BASE_URL", _FALLBACK_BASE).rstrip("/")
else:
    BASE_URL = _get("AJAIB_IOS_BASE_URL", _FALLBACK_BASE).rstrip("/")

# --- Endpoint paths --------------------------------------------------------
LOGIN_PATH = "/api/v8/login/"
PIN_VALIDATE_PATH = "/api/v4/users/me/pin/validate"
REFRESH_PATH = "/api/v7/refresh/"
USERS_ME_PATH = "/api/v3/users/me/"
ORDERBOOK_PATH = "/api/v1/stock/data/orderbook"
PRICE_DETAIL_PATH = "/api/v1/stock/detail/{code}/price/"
ORDER_BUY_PATH = "/api/v1/stock-regular/order/buy"
ORDER_SELL_PATH = "/api/v1/stock-regular/order/sell"
ORDER_LIST_PATH = "/api/v4/stock-trading/public/order/REG/list"
ORDER_WITHDRAW_PATH = "/api/v2/stock-regular/order/withdraw"
PORTFOLIO_PATH = "/api/v3/stock/portfoliodetail/"

# Liquidity injection: when a sell fails (e.g. HTTP 425 "too early" — the credit
# hasn't settled yet), top up this many lots, wait, then retry the sell once.
LIQUIDITY_INJECT_LOT = int(_get("AJAIB_LIQUIDITY_INJECT_LOT", "50000"))
LIQUIDITY_RETRY_DELAY_SECONDS = float(_get("AJAIB_LIQUIDITY_RETRY_DELAY_SECONDS", "1"))

# Internal stock-asset-ledger credit endpoint (top-up). This is an EXACT full URL
# on its own internal host — not built from BASE_URL.
TOPUP_URL = _get(
    "AJAIB_TOPUP_URL",
    "http://stock-asset-ledger-http.stg.ajaib.int"
    "/api/v1/internal/stock-asset-ledger/asset/REG/credit",
)

# --- Trading defaults ------------------------------------------------------
# Hardcoded-but-overridable stock code (change via AJAIB_STOCK_CODE).
DEFAULT_STOCK_CODE = _get("AJAIB_STOCK_CODE", "BMRI")

# Fallback "current price" used only when the order book side is empty AND the
# price-detail API is unavailable. The tick offset is applied to this reference.
# ARA/ARB always come from the price-detail API (never env).
FALLBACK_PRICE = int(_get("AJAIB_FALLBACK_PRICE", "50"))

# Max lots allowed in a single order request. In --until auto-lot mode a large
# best-level volume is split across multiple orders capped at this value.
MAX_LOT_PER_ORDER = int(_get("AJAIB_MAX_LOT_PER_ORDER", "50000"))

# --- Random trading mode (--random) ----------------------------------------
# Each order: a random 1-5 ticks above OR below the reference price, a random
# lot in [MIN, MAX], repeated every INTERVAL seconds (buy + sell in parallel).
RANDOM_LOT_MIN = int(_get("AJAIB_RANDOM_LOT_MIN", "100"))
RANDOM_LOT_MAX = int(_get("AJAIB_RANDOM_LOT_MAX", "5000"))
RANDOM_INTERVAL_SECONDS = float(_get("AJAIB_RANDOM_INTERVAL_SECONDS", "3"))
RANDOM_TICKS_MIN = int(_get("AJAIB_RANDOM_TICKS_MIN", "1"))
RANDOM_TICKS_MAX = int(_get("AJAIB_RANDOM_TICKS_MAX", "5"))

# --- Device / app identity headers -----------------------------------------
# RULE: every header value is read from env, never hardcoded in code.
# The active set is chosen by AJAIB_DEVICE_PROFILE (IOS or ANDROID).

# Shared across both profiles.
_COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Language": _get("ACCEPT_LANGUAGE", "id"),
    "Accept-Encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
    "X-Device-Signature": _get("X_DEVICE_SIGNATURE", "qa-test-automation-1"),
    "x-app-mode": _get("X_APP_MODE", "PRO"),
    "X-Product": _get("X_PRODUCT", "stock-mf"),
}

if IS_ANDROID:
    DEVICE_HEADERS = {
        **_COMMON_HEADERS,
        "x-platform": "ANDROID",
        "X-Android-Ver-Id": _get("X_ANDROID_VER_ID", "81C81887-5BD8-41D9-B0E1-1559CD6A8740"),
        "X-Android-Ver-Name": _get("X_ANDROID_VER_NAME", "6.99.0202605241939"),
        "X-Android-Ver-Code": _get("X_ANDROID_VER_CODE", "6.99.0"),
        "User-Agent": _get("USER_AGENT_ANDROID", "Android-6.99.0"),
    }
else:
    DEVICE_HEADERS = {
        **_COMMON_HEADERS,
        "X-Platform": "IOS",
        "X-IOS-Ver-Id": _get("X_IOS_VER_ID", "81C81887-5BD8-41D9-B0E1-1559CD6A8740"),
        "X-IOS-Ver-Name": _get("X_IOS_VER_NAME", "6.99.0202605241939"),
        "X-IOS-Ver-Code": _get("X_IOS_VER_CODE", "6.99.0"),
        "User-Agent": _get("USER_AGENT_IOS", "iOS-6.99.0"),
    }

