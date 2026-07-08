# CLAUDE.md

Guidance for working in this repo. Read this before making changes.

## What this is

A Python HTTP automation bot for the Ajaib stock API. It logs in, reads market
data (order book), and places/queries orders. All interaction is plain HTTP via
`requests` — no SDK.

## Working rules (IMPORTANT — these are user-established conventions)

1. **Pasted curls contribute only path, query params, and body — never headers.**
   Headers come exclusively from `config.DEVICE_HEADERS` (the active device
   profile) plus the auth token injected by the client. When the user gives a
   curl, add the endpoint as `self.client.get(path, params=...)` /
   `self.client.post(path, json=...)`. Do not copy the curl's headers.
2. **Every base URL and header value is read from env** (`config.py` via `_get`),
   never hardcoded in logic.
3. **No session is persisted.** The bot logs in fresh on every run; there is no
   `.session.json`. The refresh token lives in memory on the client.
4. **The main buy flow always executes** (no dry run). `buy_ticks_above_ask()`
   places the order every call.

## Architecture

```
main.py            CLI entry point: boot -> buy flow (order book -> +N ticks -> buy)
config.py          All env-driven config: profile switch, base URL, paths, headers
src/
  client.py        ApiClient: requests.Session wrapper. Injects headers + jwt token,
                   logs every hit to api.logs, auto-refreshes once on 401.
  auth.py          login -> PIN validate -> access token; refresh_token() on 401.
  bot.py           StockBot: get_orderbook, get_all_orders, buy, sell,
                   buy_ticks_above_ask, buy_until_price, cancel_order,
                   cancel_all_orders. _best_ask_price() parses result.ask.items.
  ticks.py         IDX tick size (fraksi harga) math: tick_size(), add_ticks().
```

### Two accounts (buy vs sell)
`config.ACCOUNT_BUY` (account 1, `AJAIB_*`) and `config.ACCOUNT_SELL` (account 2,
`AJAIB_SELL_*`, falls back to account 1 when unset/empty). `config.account_for(side)`
picks one. `StockBot(account=...)` logs that account in; `main.py` instantiates the
bot with `account_for(args.side)`, so **a buy run uses account 1, a sell run uses
account 2**. Auth functions (`login`/`validate_pin`/`refresh_token`/`authenticate`)
take the account dict.

### Auth flow (two steps, both on `config.BASE_URL`)
1. `POST /api/v8/login/` (email + password) → PIN-stage `pin_token` (`typ:PIN`).
2. `POST /api/v4/users/me/pin/validate` with `Authorization: jwt <pin_token>` +
   `User-Id` header + `{"pin_code"}` → real `access_token` (`typ:ACCESS`) and
   `refresh_token` (nested under `result`).

The access token is applied to the client as `Authorization: jwt <token>`
(Ajaib uses the `jwt` scheme, NOT `Bearer`). The refresh token is held in
`client.refresh_jwt`. After validate, `fetch_ajaib_id()` GETs `/api/v3/users/me/`
and stores the response `id` in `client.ajaib_id` (best-effort; for later use).

### 401 auto-refresh
`ApiClient.request()` catches a `401`, calls the refresh handler **once**
(`refresh_token()` → `POST /api/v7/refresh/` with the refresh token), and retries
the same request with `_allow_refresh=False` so it can never loop.

### Order book → buy
`buy_ticks_above_ask(code, lot, ticks=5)`:
order book → `_best_ask_price` (lowest price in `result.ask.items`) →
`add_ticks(best_ask, ticks)` (IDX band-aware) → `buy()`. Always executes.

### Buy until target price
`buy_until_price(code, target_price, lot=None, poll_interval=1.0, max_attempts=100)`:
loops — while best ask < target, place a limit buy at `target_price`, sleep, re-check.
Stops when best ask reaches target or `max_attempts` orders placed. `lot=None`
defaults to the **volume at the best ask** each iteration (clears one level per
order); pass a number to force a fixed lot. CLI: `--until`, `--interval`,
`--max-attempts`. `_best_ask_level`/`_best_bid_level` expose the full level (price+lot).

### Sell side (mirror of buy)
`_best_bid_price` reads `result.bid.items` (highest price = best bid).
`sell_ticks_below_bid(code, lot, ticks=5)`: best bid → `add_ticks(bid, -ticks)` → `sell()`.
`sell_until_price(...)`: while best bid > target, place a limit sell at target, until
best bid drops to target. CLI: pick the side with `--side buy|sell` (default `buy`);
`--ticks`/`--until`/`--interval`/`--max-attempts` apply to both sides.

### Portfolio check + top-up before selling
Every sell path calls `ensure_sellable_lot(code, lot, price)` first: `get_portfolio`
→ `_portfolio_lot` (reads `result.portfolio[0].lot`, 0 if the array is empty) → if the
holding is short, `top_up(code, needed_lot, price)` credits stock so the sell can go
through. The check runs on the sell account (account 2).

`top_up` POSTs to `config.TOPUP_URL` (exact internal URL, NOT built from BASE_URL):
`shares = lot * 100 * 10`, `price` = target price, `source = "TRADING_SERVICE"`,
`source_id = "<code>_<6 digits>_<10 digits>"` (randomized), and header
`User-Id = client.ajaib_id` (from login /users/me).

## Device profile switch

`AJAIB_DEVICE_PROFILE` (`IOS` | `ANDROID`) in `.env` is one switch controlling:
- the header set (`X-IOS-Ver-*` + `X-Platform:IOS` vs `X-Android-Ver-*` +
  `x-platform:ANDROID`, and the User-Agent),
- the login payload `platform` field,
- the base URL (`AJAIB_IOS_BASE_URL` vs `AJAIB_ANDROID_BASE_URL`,
  falling back to `AJAIB_BASE_URL`).

`load_dotenv()` does not override real env vars, so a one-off is possible:
`AJAIB_DEVICE_PROFILE=ANDROID python main.py`.

## IDX tick sizes (fraksi harga)

Tick size depends on the price band, so `add_ticks` steps one tick at a time:
`<200 → 1`, `200–<500 → 2`, `500–<2000 → 5`, `2000–<5000 → 10`, `≥5000 → 25`.

## Run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in credentials / pick profile
python main.py --code BMRI            # buy 1 lot, 5 ticks above best ask
python main.py --code BMRI --dry-run  # compute price, send nothing
python main.py --lot 2 --ticks 3
```

## Logging

Every request + response is appended to `api.logs` in reproducible curl form
(`_to_curl` in `client.py`). It contains real tokens/credentials — git-ignored,
treat as sensitive.

## Gotchas

- **Cloudflare**: the hosts sit behind Cloudflare bot protection. Repeated calls
  from a datacenter IP get 403 "you have been blocked" (an HTML page, not JSON).
  This is IP reputation, not a code bug. Real requests need a valid `__cf_bm`
  cookie / a trusted IP.
- **Order list status filter is loose**: a `status=OPEN` request can return mixed
  statuses. For cancel logic, trust the per-order **`is_cancelable`** boolean, not
  the status filter.
- **Unconfirmed response shapes**: token extraction (`_extract_token`) and the
  order-book parser key off observed payloads; if a response nests differently,
  the code raises with the raw payload so the field can be pinned.

## Orders & cancel

- `get_all_orders(status="OPEN")` — paginated list via `result.count`.
- `cancel_order(ticker_code, orderno)` — withdraw one order. `orderno` is the
  list's `vendor_order_id`; `ticker_code` is its `stock_code`.
- `cancel_all_orders()` — withdraws every order with `is_cancelable == True`,
  capturing per-order errors instead of aborting the batch.

## Not yet wired into main.py

- `sell()`, `get_all_orders()`, `cancel_order()`, `cancel_all_orders()` exist as
  functions but are not called from `main.py` (call them from the testing
  section or your own flow).
