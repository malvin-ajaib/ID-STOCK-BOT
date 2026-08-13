"""Trading bot: market data + order actions on top of the authenticated client.

StockBot wires auth, the order book, order listing, and buy/sell. The headline
flow is buy_ticks_above_ask(): order book -> best ask -> +N ticks -> buy.
"""
from __future__ import annotations

import random
import time
from typing import Any

import config
from src.auth import ensure_authenticated, refresh_token
from src.client import ApiClient, ApiError
from src.ticks import add_ticks


def _best_ask_price(orderbook: Any) -> int:
    """Extract the best (lowest) ask price from ``result.ask.items``."""
    return int(_best_ask_level(orderbook)["price"])


def _side_levels(orderbook: Any, side: str) -> list:
    """Return the ``result.<side>.items`` list (side = 'ask' or 'bid')."""
    result = orderbook.get("result") if isinstance(orderbook, dict) else None
    node = result.get(side) if isinstance(result, dict) else None
    items = node.get("items") if isinstance(node, dict) else None
    levels = [
        level
        for level in (items or [])
        if isinstance(level, dict) and isinstance(level.get("price"), (int, float))
    ]
    if not levels:
        raise RuntimeError(
            f"Could not read result.{side}.items in the order book. "
            f"Payload:\n{orderbook}"
        )
    return levels


def _best_ask_level(orderbook: Any) -> dict:
    """Best ask level ``{"price", "lot", ...}`` — lowest-priced ask."""
    return min(_side_levels(orderbook, "ask"), key=lambda lv: lv["price"])


def _best_bid_level(orderbook: Any) -> dict:
    """Best bid level ``{"price", "lot", ...}`` — highest-priced bid."""
    return max(_side_levels(orderbook, "bid"), key=lambda lv: lv["price"])


def _best_bid_price(orderbook: Any) -> int:
    """Extract the best (highest) bid price from ``result.bid.items``."""
    return int(_best_bid_level(orderbook)["price"])


def _level_lot(level: dict) -> int:
    """Volume (lot) available at an order-book level; falls back to 1."""
    lot = level.get("lot")
    return int(lot) if isinstance(lot, (int, float)) and lot > 0 else 1


def _portfolio_lot(response: Any) -> int:
    """Owned lot from ``result.portfolio[0].lot``; 0 when the portfolio is empty."""
    result = response.get("result") if isinstance(response, dict) else None
    portfolio = result.get("portfolio") if isinstance(result, dict) else None
    if not isinstance(portfolio, list) or not portfolio:
        return 0  # no holding for this ticker
    first = portfolio[0]
    lot = first.get("lot") if isinstance(first, dict) else None
    return int(lot) if isinstance(lot, (int, float)) else 0


def _random_offset_ticks() -> int:
    """Random 1-5 (config range) ticks, randomly above (+) or below (-)."""
    magnitude = random.randint(config.RANDOM_TICKS_MIN, config.RANDOM_TICKS_MAX)
    return magnitude * random.choice((1, -1))


def _random_lot() -> int:
    """Random lot in the configured [MIN, MAX] range."""
    return random.randint(config.RANDOM_LOT_MIN, config.RANDOM_LOT_MAX)


class StockBot:
    def __init__(self, account: dict | None = None):
        self.account = account if account is not None else config.ACCOUNT_BUY
        self.client = ApiClient()
        # On a 401, refresh the token once and retry the request automatically.
        self.client.set_refresh_handler(
            lambda: refresh_token(self.client, self.account)
        )

    def boot(self, force_login: bool = False) -> dict:
        """Authenticate on startup — logs in fresh every run (no caching)."""
        session = ensure_authenticated(self.client, self.account, force=force_login)
        if not self.client.is_authenticated:
            raise RuntimeError(
                "Login succeeded but no token was found in the response. "
                "Adjust src/auth.py:_extract_token() to match the payload."
            )
        return session

    # -- market data --------------------------------------------------------
    def get_orderbook(self, code: str | None = None) -> Any:
        """Fetch the order book for a stock code from the PLE (market data) host.

        ``code`` defaults to config.DEFAULT_STOCK_CODE (env AJAIB_STOCK_CODE).
        """
        code = code or config.DEFAULT_STOCK_CODE
        # Uses the client's global base URL (PLE host).
        return self.client.get(config.ORDERBOOK_PATH, params={"code": code})

    # -- portfolio ----------------------------------------------------------
    def get_portfolio(self, ticker_code: str) -> Any:
        """Fetch this account's portfolio detail for a ticker."""
        return self.client.get(
            config.PORTFOLIO_PATH, params={"ticker_code": ticker_code}
        )

    def owned_lot(self, ticker_code: str) -> int:
        """How many lots of ``ticker_code`` this account currently holds (0 if none)."""
        return _portfolio_lot(self.get_portfolio(ticker_code))

    def ensure_sellable_lot(self, ticker_code: str, needed_lot: int, price: int) -> dict:
        """Make sure this account holds >= ``needed_lot`` before selling.

        Checks the portfolio; if the holding is short, tops up so the sell can go
        through. ``price`` is the price we want to reach (used by the top-up).
        Returns a summary of the check.
        """
        owned = self.owned_lot(ticker_code)
        shortfall = max(needed_lot - owned, 0)
        result = {
            "ticker_code": ticker_code,
            "needed": needed_lot,
            "owned": owned,
            "shortfall": shortfall,
            "topped_up": False,
        }
        if shortfall > 0:
            result["topup_response"] = self.top_up(ticker_code, needed_lot, price)
            result["topped_up"] = True
        return result

    def top_up(self, ticker_code: str, lot: int, price: int) -> Any:
        """Credit stock to this account via the internal stock-asset-ledger.

        shares = lot * 100 * 10; price = the price we want to reach; User-Id is
        this account's ajaib_id (from login). source_id is randomized as
        ``<stock_code>_<6 digits>_<10 digits>``.
        """
        user_id = self.client.ajaib_id
        if not user_id:
            raise RuntimeError("Cannot top up: ajaib_id is not set (login first).")

        source_id = (
            f"{ticker_code}_{random.randint(100000, 999999)}"
            f"_{random.randint(1000000000, 9999999999)}"
        )
        payload = {
            "stock_code": ticker_code,
            "source": "TRADING_SERVICE",
            "source_id": source_id,
            "shares": lot * 100 * 10,
            "price": price,
        }
        # Exact internal URL; User-Id comes from the logged-in account's ajaib_id.
        return self.client.post(
            config.TOPUP_URL, json=payload, headers={"User-Id": str(user_id)}
        )

    def place_sell(self, code: str, lot: int, price: int) -> Any:
        """Sell ``lot`` @ ``price``, ensuring/injecting liquidity as needed.

        1. Pre-check the portfolio and top up any shortfall.
        2. Try the sell. If it fails (e.g. HTTP 425 — the credit hasn't settled),
           inject a big liquidity chunk (`LIQUIDITY_INJECT_LOT`), wait, and retry
           the sell ONCE. A second failure propagates.

        Every sell path goes through here.
        """
        self.ensure_sellable_lot(code, lot, price)
        try:
            return self.sell(code, lot=lot, price=price)
        except ApiError as exc:
            print(
                f"{code}: sell failed ({exc}); injecting "
                f"{config.LIQUIDITY_INJECT_LOT} lot and retrying once."
            )
            self.top_up(code, config.LIQUIDITY_INJECT_LOT, price)
            if config.LIQUIDITY_RETRY_DELAY_SECONDS > 0:
                time.sleep(config.LIQUIDITY_RETRY_DELAY_SECONDS)
            return self.sell(code, lot=lot, price=price)

    # -- orders -------------------------------------------------------------
    def get_all_orders(
        self,
        status: str = "OPEN",
        side: str = "ALL",
        page_size: int = 20,
        max_pages: int = 100,
    ) -> list:
        """Return every order matching ``status``, following pagination.

        Loops pages until ``result.count`` orders are collected (or a page comes
        back empty). ``max_pages`` is a safety cap against an unexpected count.
        """
        orders: list = []
        page = 1
        while page <= max_pages:
            resp = self.client.get(
                config.ORDER_LIST_PATH,
                params={
                    "page": page,
                    "page_size": page_size,
                    "side": side,
                    "status": status,
                },
            )
            result = resp.get("result", {}) if isinstance(resp, dict) else {}
            items = result.get("results", []) or []
            orders.extend(items)

            count = result.get("count", 0)
            if not items or len(orders) >= count:
                break
            page += 1
        return orders

    def cancel_order(self, ticker_code: str, orderno: str) -> Any:
        """Withdraw (cancel) a single order by its vendor order number."""
        payload = {"ticker_code": ticker_code, "orderno": orderno}
        return self.client.post(config.ORDER_WITHDRAW_PATH, json=payload)

    def cancel_all_orders(self) -> list:
        """Cancel every cancelable open order.

        Lists OPEN orders and withdraws each one with ``is_cancelable == True``
        (the reliable signal — the status filter alone can return mixed states).
        Returns a per-order result list; a failed cancel is captured, not raised,
        so one error doesn't stop the rest.
        """
        results = []
        for order in self.get_all_orders(status="OPEN"):
            if not order.get("is_cancelable"):
                continue
            ticker_code = order.get("stock_code")
            orderno = order.get("vendor_order_id")
            entry = {"ticker_code": ticker_code, "orderno": orderno}
            try:
                entry["response"] = self.cancel_order(ticker_code, orderno)
            except ApiError as exc:
                entry["error"] = {"status_code": exc.status_code, "body": exc.body}
            results.append(entry)
        return results

    # -- composite flow -----------------------------------------------------
    def buy_ticks_above_ask(self, code: str, lot: int, ticks: int = 5) -> dict:
        """Order book -> best ask -> +N ticks -> buy at that price.

        Always places the order. Returns a summary dict including the response.
        """
        orderbook = self.get_orderbook(code)
        best_ask = _best_ask_price(orderbook)
        target_price = add_ticks(best_ask, ticks)

        return {
            "code": code,
            "lot": lot,
            "ticks": ticks,
            "best_ask": best_ask,
            "target_price": target_price,
            "order_response": self.buy(code, lot=lot, price=target_price),
        }

    def buy_until_price(
        self,
        code: str,
        target_price: int,
        lot: int | None = None,
        *,
        poll_interval: float = 1.0,
        max_attempts: int = 100,
    ) -> dict:
        """Keep placing limit buys at ``target_price`` until the market reaches it.

        Each loop reads the order book; while the best ask is still below
        ``target_price`` it places a limit buy at ``target_price`` (which lifts
        the book toward the target), waits ``poll_interval`` seconds, and checks
        again. Stops when the best ask has reached ``target_price`` or after
        ``max_attempts`` orders (safety cap).

        ``lot`` defaults to the volume sitting at the best ask each iteration
        (so one order clears one price level); pass a number to force a fixed lot.
        """
        attempts: list = []
        reached = False

        for _ in range(max_attempts):
            level = _best_ask_level(self.get_orderbook(code))
            best_ask = int(level["price"])
            if best_ask >= target_price:
                reached = True
                break

            order_lot = lot if lot is not None else _level_lot(level)
            order_lot = min(order_lot, config.MAX_LOT_PER_ORDER)
            response = self.buy(code, lot=order_lot, price=target_price)
            attempts.append({"best_ask": best_ask, "lot": order_lot, "response": response})
            print(
                f"{code}: best ask {best_ask} < target {target_price} "
                f"-> bought {order_lot} lot @ {target_price} (attempt {len(attempts)})"
            )
            time.sleep(poll_interval)
        else:
            reached = _best_ask_price(self.get_orderbook(code)) >= target_price

        return {
            "code": code,
            "target_price": target_price,
            "reached": reached,
            "orders_placed": len(attempts),
            "attempts": attempts,
        }

    def sell_ticks_below_bid(self, code: str, lot: int, ticks: int = 5) -> dict:
        """Order book -> best bid -> -N ticks -> sell at that price.

        Mirror of buy_ticks_above_ask. Always places the order.
        """
        orderbook = self.get_orderbook(code)
        best_bid = _best_bid_price(orderbook)
        target_price = add_ticks(best_bid, -ticks)

        return {
            "code": code,
            "lot": lot,
            "ticks": ticks,
            "best_bid": best_bid,
            "target_price": target_price,
            "order_response": self.place_sell(code, lot, target_price),
        }

    def sell_until_price(
        self,
        code: str,
        target_price: int,
        lot: int | None = None,
        *,
        poll_interval: float = 1.0,
        max_attempts: int = 100,
    ) -> dict:
        """Keep placing limit sells at ``target_price`` until the market reaches it.

        Mirror of buy_until_price. Each loop reads the order book; while the best
        bid is still above ``target_price`` it places a limit sell at
        ``target_price`` (which pushes the book down toward the target), waits
        ``poll_interval`` seconds, and checks again. Stops when the best bid has
        dropped to ``target_price`` or after ``max_attempts`` orders (safety cap).

        ``lot`` defaults to the volume sitting at the best bid each iteration
        (so one order clears one price level); pass a number to force a fixed lot.
        """
        attempts: list = []
        reached = False

        for _ in range(max_attempts):
            level = _best_bid_level(self.get_orderbook(code))
            best_bid = int(level["price"])
            if best_bid <= target_price:
                reached = True
                break

            order_lot = lot if lot is not None else _level_lot(level)
            order_lot = min(order_lot, config.MAX_LOT_PER_ORDER)
            response = self.place_sell(code, order_lot, target_price)
            attempts.append({"best_bid": best_bid, "lot": order_lot, "response": response})
            print(
                f"{code}: best bid {best_bid} > target {target_price} "
                f"-> sold {order_lot} lot @ {target_price} (attempt {len(attempts)})"
            )
            time.sleep(poll_interval)
        else:
            reached = _best_bid_price(self.get_orderbook(code)) <= target_price

        return {
            "code": code,
            "target_price": target_price,
            "reached": reached,
            "orders_placed": len(attempts),
            "attempts": attempts,
        }

    # -- random single orders (used by --random threads) --------------------
    def random_buy(self, code: str) -> dict:
        """One buy at a random ±1-5 ticks off the best ask, random lot."""
        best_ask = _best_ask_price(self.get_orderbook(code))
        offset = _random_offset_ticks()
        price = add_ticks(best_ask, offset)
        lot = _random_lot()
        return {
            "side": "BUY",
            "code": code,
            "reference": best_ask,
            "offset_ticks": offset,
            "lot": lot,
            "price": price,
            "response": self.buy(code, lot=lot, price=price),
        }

    def random_sell(self, code: str) -> dict:
        """One sell at a random ±1-5 ticks off the best bid, random lot.

        Tops up the holding first if short (same guard as the other sell flows).
        """
        best_bid = _best_bid_price(self.get_orderbook(code))
        offset = _random_offset_ticks()
        price = add_ticks(best_bid, offset)
        lot = _random_lot()
        return {
            "side": "SELL",
            "code": code,
            "reference": best_bid,
            "offset_ticks": offset,
            "lot": lot,
            "price": price,
            "response": self.place_sell(code, lot, price),
        }

    # -- trading actions ----------------------------------------------------
    # NOTE: buy() is implemented but intentionally NOT wired into the main flow.

    def buy(
        self,
        ticker_code: str,
        lot: int,
        price: int,
        *,
        board: str = "0RG",
        period: str = "day",
        trading_mode: str = "PRO",
        pooling_eligible: bool = True,
    ) -> Any:
        """Place a regular buy order on the PLE host."""
        payload = {
            "metadata": {"pooling_eligible": pooling_eligible},
            "lot": lot,
            "period": period,
            "ticker_code": ticker_code,
            "board": board,
            "price": price,
            "trading_mode": trading_mode,
        }
        return self.client.post(config.ORDER_BUY_PATH, json=payload)

    def sell(
        self,
        ticker_code: str,
        lot: int,
        price: int,
        *,
        board: str = "0RG",
        period: str = "day",
        trading_mode: str = "PRO",
        pooling_eligible: bool = True,
    ) -> Any:
        """Place a regular sell order on the PLE host."""
        payload = {
            "board": board,
            "period": period,
            "ticker_code": ticker_code,
            "trading_mode": trading_mode,
            "lot": lot,
            "metadata": {"pooling_eligible": pooling_eligible},
            "price": price,
        }
        return self.client.post(config.ORDER_SELL_PATH, json=payload)
