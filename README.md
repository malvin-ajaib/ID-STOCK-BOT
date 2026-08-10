# id-stock-bot

A Python automation bot for the Ajaib stock API over plain HTTP (`requests`).
It logs in (login → PIN validate), reads the order book, and places/queries/cancels
orders. Every run logs in fresh — no session is stored on disk.

## ⚡ Command cheat sheet

> First time? Do the [Setup](#setup) once, then `source venv/bin/activate`.
> `--code` defaults to `AJAIB_STOCK_CODE` in `.env`. Orders are placed for real.

```bash
# BUY: 1 lot, 5 ticks above best ask (default flow)
python main.py --code BMRI

# BUY: 2 lots, 3 ticks above best ask
python main.py --code BMRI --lot 2 --ticks 3

# SELL: 1 lot, 5 ticks below best bid  (uses account 2)
python main.py --code BMRI --side sell

# SELL: 2 lots, 3 ticks below best bid
python main.py --code BMRI --side sell --lot 2 --ticks 3

# BUY at an EXACT price + lot (one order)
python main.py --code BMRI --price 4300 --lot 2

# SELL at an EXACT price + lot (tops up account 2 if short)
python main.py --code BMRI --side sell --price 4300 --lot 2

# BUY UNTIL price reached: keep buying at 4000 until best ask hits 4000
#   (lot omitted -> uses the volume at the best ask each step)
python main.py --code BMRI --until 4000

# SELL UNTIL price reached: keep selling at 4000 until best bid drops to 4000
python main.py --code BMRI --side sell --until 4000

# UNTIL with fixed lot + faster polling + higher order cap
python main.py --code BMRI --until 4000 --lot 5 --interval 0.5 --max-attempts 200

# RANDOM mode: buy (acct 1) + sell (acct 2) random orders in parallel, until Ctrl+C
python main.py --code BMRI --random

# Force a fresh login (all runs log in fresh anyway)
python main.py --code BMRI --force-login

# One-off device profile switch (no .env edit)
AJAIB_DEVICE_PROFILE=IOS python main.py --code BMRI

# Full option list
python main.py --help
```

| Flag | Default | Applies to | Meaning |
|------|---------|-----------|---------|
| `--code` | `AJAIB_STOCK_CODE` | all | Stock code to trade |
| `--side` | `buy` | ticks / until | `buy` (acct 1) or `sell` (acct 2) |
| `--lot` | 1 (ticks) / auto (until) | ticks / until | Lots per order; until auto = best level volume |
| `--ticks` | 5 | ticks flow | Ticks above ask / below bid |
| `--price` | — | exact flow | Place ONE order at this exact price |
| `--until` | — | until flow | Target price; loop until market reaches it |
| `--interval` | 1.0 | until flow | Seconds between attempts |
| `--max-attempts` | 100 | until flow | Order cap for the until loop |
| `--random` | off | random mode | Parallel random buy+sell until Ctrl+C |
| `--force-login` | off | all | Re-authenticate explicitly |

Functions not on the CLI (`get_all_orders`, `cancel_order`, `cancel_all_orders`,
`get_portfolio`, …) — see [Use the other actions from Python](#use-the-other-actions-from-python).

## Features

- Two-step auth: `login` → `pin/validate` → `access_token` (`jwt` scheme)
- Auto token **refresh once on 401**, then retries the request
- Order book read, buy, sell, list orders, cancel order, cancel-all
- Headline flow: order book → best ask → **+5 ticks** → buy (IDX tick-aware)
- One switch for **iOS / Android** device profile (headers + base URL + platform)
- Every request + response logged to `api.logs` in curl form
- All credentials, URLs, and headers come from `.env` — nothing hardcoded

## Setup

Uses a virtual environment (venv).

```bash
cd /Users/malvin/Documents/id-stock-bot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env              # then edit .env (see below)
```

### Configure `.env`

| Key | What it is |
|-----|------------|
| `AJAIB_EMAIL` / `AJAIB_PASSWORD` / `AJAIB_PIN_CODE` | account 1 (BUY) credentials |
| `AJAIB_USER_ID` | account 1 `User-Id` header |
| `AJAIB_SELL_EMAIL` / `AJAIB_SELL_PASSWORD` / `AJAIB_SELL_PIN_CODE` / `AJAIB_SELL_USER_ID` | account 2 (SELL); unset → reuse account 1 |
| `AJAIB_DEVICE_PROFILE` | `IOS` or `ANDROID` — the device profile switch |
| `AJAIB_IOS_BASE_URL` / `AJAIB_ANDROID_BASE_URL` | base URL per profile |
| `AJAIB_STOCK_CODE` | default stock code (e.g. `BMRI`) |
| `X_*`, `USER_AGENT_*` | device/app headers per profile |

`.env` is git-ignored — never commit it.

## How to use

### Run the main flow (ticks above ask / below bid)

```bash
python main.py --code BMRI                  # BUY 1 lot, 5 ticks above best ask
python main.py --code BMRI --side sell       # SELL 1 lot, 5 ticks below best bid
python main.py --code BMRI --lot 2 --ticks 3
python main.py --force-login                 # (all runs log in fresh anyway)
```

`--side buy` (default) references the best **ask** and adds ticks; `--side sell`
references the best **bid** and subtracts ticks. **Buy runs use account 1, sell runs
use account 2** (see the `.env` table). The main flow **always places the order**
(no dry run):

```
Login OK.
  token: eyJ0eXAi…AbC123

BMRI: best ask 4260 +5 ticks -> buy 1 lot @ 4310
Order sent. Response:
{ ... }
```

If `--code` is omitted it falls back to `AJAIB_STOCK_CODE`.

### Keep trading until a target price is reached

Repeatedly place limit orders at a target price until the market reaches it
(useful for driving/observing order matching in the test env):

```bash
# keep BUYING BMRI at 4000 until the best ask reaches 4000
python main.py --code BMRI --until 4000
# keep SELLING BMRI at 4000 until the best bid drops to 4000
python main.py --code BMRI --side sell --until 4000
python main.py --code BMRI --until 4000 --lot 2 --interval 0.5 --max-attempts 200
```

Each loop reads the order book. **Buy:** while the best ask is *below* the target,
it keeps buying at the target (lifting the book up). **Sell:** while the best bid is
*above* the target, it keeps selling at the target (pushing the book down). Stops
when the market reaches the target or after `--max-attempts` orders (safety cap).

In `--until` mode, **`--lot` defaults to the volume sitting at the best ask (buy) /
best bid (sell)** each iteration — so one order clears one price level and the book
walks toward the target. Pass `--lot N` to force a fixed size instead.

### Random mode (buy + sell in parallel)

```bash
python main.py --code BMRI --random
```

Runs two threads at once — **buy on account 1, sell on account 2** — each placing
one random order every few seconds until you Ctrl+C. Each order is a random **±1–5
ticks** off the best ask (buy) / best bid (sell), with a random **lot in 100–5000**.
All knobs are env-configurable:

```
AJAIB_RANDOM_LOT_MIN=100
AJAIB_RANDOM_LOT_MAX=5000
AJAIB_RANDOM_TICKS_MIN=1
AJAIB_RANDOM_TICKS_MAX=5
AJAIB_RANDOM_INTERVAL_SECONDS=3
```

Random sells still run the portfolio check + top-up, so the sell account is
funded automatically.

### Switch iOS ⇄ Android

Flip one line in `.env`:

```
AJAIB_DEVICE_PROFILE=IOS      # or ANDROID
```

This changes the header set, the login `platform` field, and the base URL
(`AJAIB_IOS_BASE_URL` vs `AJAIB_ANDROID_BASE_URL`) together. One-off without
editing `.env`:

```bash
AJAIB_DEVICE_PROFILE=ANDROID python main.py --code BMRI
```

### Use the other actions from Python

Not every function is on the CLI. Call them via `StockBot`:

```python
import config
from src.bot import StockBot

bot = StockBot()
bot.boot()                              # log in

bot.get_orderbook("BMRI")               # raw order book
bot.buy("BMRI", lot=1, price=4310)      # place a buy
bot.sell("BBCA", lot=1, price=8750)     # place a sell
bot.buy_ticks_above_ask("BMRI", lot=1, ticks=5)   # the main flow

orders = bot.get_all_orders(status="OPEN")        # paginated list
bot.cancel_order("BMRI", "TA-01-0617KOM8X")       # cancel one (orderno = vendor_order_id)
bot.cancel_all_orders()                           # cancel every cancelable open order

bot.get_portfolio("BBCA")                         # raw portfolio detail
bot.owned_lot("BBCA")                             # lots held (0 if none)

sell_bot = StockBot(account=config.ACCOUNT_SELL)  # act as account 2 explicitly
sell_bot.boot()
```

## Project layout

```
main.py       CLI entry point (login -> buy 5 ticks above ask)
config.py     env-driven config: profile switch, base URL, paths, headers
src/
  client.py   ApiClient: requests wrapper, header/token injection, 401 refresh, logging
  auth.py     login -> PIN validate -> access token; refresh_token()
  bot.py      StockBot: orderbook, orders, buy/sell, cancel, buy_ticks_above_ask
  ticks.py    IDX tick size (fraksi harga) math
```

## Logging

Every request and its response are appended to `api.logs` as a reproducible curl
command. **It contains real tokens/credentials** — git-ignored, treat as sensitive.

## Notes

- Behind **Cloudflare**: repeated calls from a datacenter IP can get a 403
  "you have been blocked" HTML page. That's IP reputation, not a code bug — a
  valid `__cf_bm` cookie / trusted IP is needed.
- The `status=OPEN` order filter can return mixed statuses; cancel logic trusts
  the per-order `is_cancelable` flag instead.
- Use only against accounts and environments you're authorized to access.
