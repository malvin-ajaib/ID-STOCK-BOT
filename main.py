"""Entry point.

Logs in fresh on every run (no session cached). Pass --force-login to be explicit.

Main flow: auto login -> read order book -> buy 5 ticks above the best ask.
The order is always placed.

Usage:
    python main.py --code BBCA          # buy 1 lot, 5 ticks above best ask
    python main.py --lot 2 --ticks 5
    python main.py --force-login
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import config
from src.bot import StockBot
from src.client import ApiError


def run_random_trading(code: str) -> int:
    """Random-order mode: buy (account 1) and sell (account 2) in parallel threads.

    Each thread places one random order (±1-5 ticks off the reference, random
    lot) every RANDOM_INTERVAL_SECONDS until interrupted (Ctrl+C).
    """
    buy_bot = StockBot(account=config.ACCOUNT_BUY)
    sell_bot = StockBot(account=config.ACCOUNT_SELL)

    for label, bot in (("buy", buy_bot), ("sell", sell_bot)):
        try:
            bot.boot()
        except ApiError as exc:
            print(f"[{label} login failed] {exc}", file=sys.stderr)
            return 1
    print(
        f"Random trading {code}: lot {config.RANDOM_LOT_MIN}-{config.RANDOM_LOT_MAX}, "
        f"±{config.RANDOM_TICKS_MIN}-{config.RANDOM_TICKS_MAX} ticks, every "
        f"{config.RANDOM_INTERVAL_SECONDS}s. Ctrl+C to stop.\n"
    )

    stop = threading.Event()

    def loop(bot: StockBot, action_name: str, label: str) -> None:
        action = getattr(bot, action_name)
        while not stop.is_set():
            try:
                r = action(code)
                print(
                    f"[{label}] {r['side']} {r['lot']} lot @ {r['price']} "
                    f"(ref {r['reference']}, {r['offset_ticks']:+d} ticks)"
                )
            except Exception as exc:  # keep the thread alive on any error
                print(f"[{label}] error: {exc}", file=sys.stderr)
            stop.wait(config.RANDOM_INTERVAL_SECONDS)

    threads = [
        threading.Thread(target=loop, args=(buy_bot, "random_buy", "BUY"), daemon=True),
        threading.Thread(target=loop, args=(sell_bot, "random_sell", "SELL"), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping…")
        stop.set()
    for t in threads:
        t.join()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ID stock automation bot")
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Force a fresh login even if a cached session exists.",
    )
    parser.add_argument(
        "--code",
        default=config.DEFAULT_STOCK_CODE,
        help=f"Stock code for the order book. If omitted, you'll be prompted "
    )
    parser.add_argument(
        "--side",
        choices=["buy", "sell"],
        default="buy",
        help="buy = N ticks above best ask; sell = N ticks below best bid.",
    )
    parser.add_argument(
        "--lot",
        type=int,
        default=None,
        help="Lots to trade. Ticks flow defaults to 1; --until defaults to the "
        "volume at the best ask/bid each iteration.",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=5,
        help="Ticks above best ask (buy) / below best bid (sell). Default: 5.",
    )
    parser.add_argument(
        "--price",
        type=int,
        default=None,
        help="Place ONE order at this exact price (with --lot, --side). "
        "Skips the ticks flow.",
    )
    parser.add_argument(
        "--until",
        type=int,
        default=None,
        help="Target price: keep trading at this price until the market reaches it.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between attempts in --until mode (default: 1.0).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=100,
        help="Max orders to place in --until mode (default: 100).",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="Random mode: buy (acct 1) + sell (acct 2) random orders in parallel.",
    )
    args = parser.parse_args()

    # Random mode: parallel buy/sell threads, needs only the stock code.
    if args.random:
        return run_random_trading(args.code)

    # Account 1 (buy) or account 2 (sell), chosen by --side.
    account = config.account_for(args.side)
    bot = StockBot(account=account)

    try:
        session = bot.boot(force_login=args.force_login)
    except ApiError as exc:
        print(f"[login failed] {exc}", file=sys.stderr)
        print(f"  response body: {exc.body}", file=sys.stderr)
        return 1

    token = session.get("token", "")
    masked = f"{token[:8]}…{token[-6:]}" if token and len(token) > 16 else token
    print(f"Login OK ({args.side} account: {account['email']}).")
    print(f"  token: {masked}")
    print(f"  ajaib_id: {session.get('ajaib_id')}")

    # --- resolve stock code ----------------------------------------------
    code = args.code

    # --- testing section ------------------------------------------------
    
    # try:
    #     # orders = bot.get_all_orders();
    #     # print(json.dumps(orders, indent=2, ensure_ascii=False))
    #     bot.cancel_all_orders()
    # except ApiError as exc:
    #     print(f"  response body: {exc.body}", file=sys.stderr)
    #     return 1
    # return 0

    # --- keep trading until target price is reached ----------------------
    if args.until is not None:
        until_fn = bot.buy_until_price if args.side == "buy" else bot.sell_until_price
        try:
            result = until_fn(
                code,
                target_price=args.until,
                lot=args.lot,  # None -> use best ask/bid volume each iteration
                poll_interval=args.interval,
                max_attempts=args.max_attempts,
            )
        except ApiError as exc:
            print(f"[{args.side}-until failed] {exc}", file=sys.stderr)
            print(f"  response body: {exc.body}", file=sys.stderr)
            return 1

        status = "reached" if result["reached"] else "NOT reached (cap hit)"
        print(
            f"\n{code}: {args.side} target {result['target_price']} {status} after "
            f"{result['orders_placed']} order(s)."
        )
        return 0

    # --- exact price: one order at --price with --lot ---------------------
    if args.price is not None:
        lot = args.lot if args.lot is not None else 1
        try:
            if args.side == "buy":
                response = bot.buy(code, lot=lot, price=args.price)
            else:
                # Top up the sell account if the holding is short.
                bot.ensure_sellable_lot(code, lot, args.price)
                response = bot.sell(code, lot=lot, price=args.price)
        except ApiError as exc:
            print(f"[{args.side} failed] {exc}", file=sys.stderr)
            print(f"  response body: {exc.body}", file=sys.stderr)
            return 1

        print(f"\n{code}: {args.side} {lot} lot @ {args.price} (exact price)")
        print("Order sent. Response:")
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return 0

    # --- ticks flow: order book -> best ask/bid -> +/-N ticks -> order ----
    lot = args.lot if args.lot is not None else 1
    try:
        if args.side == "buy":
            result = bot.buy_ticks_above_ask(code, lot=lot, ticks=args.ticks)
            ref_label, ref_val, arrow = "best ask", result["best_ask"], f"+{args.ticks}"
        else:
            result = bot.sell_ticks_below_bid(code, lot=lot, ticks=args.ticks)
            ref_label, ref_val, arrow = "best bid", result["best_bid"], f"-{args.ticks}"
    except ApiError as exc:
        print(f"[{args.side} flow failed] {exc}", file=sys.stderr)
        print(f"  response body: {exc.body}", file=sys.stderr)
        return 1

    print(
        f"\n{code}: {ref_label} {ref_val} {arrow} ticks "
        f"-> {args.side} {result['lot']} lot @ {result['target_price']}"
    )
    print("Order sent. Response:")
    print(json.dumps(result["order_response"], indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
