"""Bybit v5 unified_trading wrapper (linear, one-way mode).

pybit HTTP is synchronous; calls are offloaded to threads so the asyncio event
loop is never blocked. In DRY_RUN no order-mutating call is sent — they are
logged and a synthetic response is returned.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Callable, Optional

from pybit.unified_trading import HTTP, WebSocket

from config import settings
from .sizing import SymbolFilters

log = logging.getLogger("bybit")

CATEGORY = "linear"


class BybitClient:
    def __init__(self) -> None:
        self.dry_run = not settings.places_orders
        self.testnet = settings.use_testnet_endpoint
        self.http = HTTP(
            testnet=self.testnet,
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
        )
        self._filters_cache: dict[str, SymbolFilters] = {}
        self._ws: Optional[WebSocket] = None

    # ---------------- account ----------------
    async def get_equity(self) -> Decimal:
        def _call():
            return self.http.get_wallet_balance(accountType="UNIFIED")

        resp = await asyncio.to_thread(_call)
        lst = resp["result"]["list"]
        if not lst:
            return Decimal(0)
        acct = lst[0]
        # totalEquity is the wallet-wide equity in USD terms
        return Decimal(str(acct.get("totalEquity") or acct.get("totalWalletBalance") or 0))

    async def get_available_margin(self) -> Decimal:
        def _call():
            return self.http.get_wallet_balance(accountType="UNIFIED")

        resp = await asyncio.to_thread(_call)
        lst = resp["result"]["list"]
        if not lst:
            return Decimal(0)
        acct = lst[0]
        val = acct.get("totalAvailableBalance") or acct.get("totalMarginBalance") or 0
        return Decimal(str(val))

    # ---------------- instrument filters ----------------
    async def get_filters(self, symbol: str) -> SymbolFilters:
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]

        def _call():
            return self.http.get_instruments_info(category=CATEGORY, symbol=symbol)

        resp = await asyncio.to_thread(_call)
        items = resp["result"]["list"]
        if not items:
            raise ValueError(f"unknown symbol {symbol}")
        it = items[0]
        lot = it["lotSizeFilter"]
        price_f = it["priceFilter"]
        lev_f = it["leverageFilter"]
        min_notional = lot.get("minNotionalValue") or lot.get("minOrderAmt") or "5"
        f = SymbolFilters(
            qty_step=Decimal(str(lot["qtyStep"])),
            min_order_qty=Decimal(str(lot["minOrderQty"])),
            min_notional=Decimal(str(min_notional)),
            tick_size=Decimal(str(price_f["tickSize"])),
            max_leverage=int(float(lev_f["maxLeverage"])),
            maintenance_margin_rate=Decimal("0.005"),
        )
        self._filters_cache[symbol] = f
        return f

    # ---------------- leverage & margin ----------------
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if self.dry_run:
            log.info("[DRY] set_leverage %s x%s", symbol, leverage)
            return

        def _call():
            try:
                return self.http.set_leverage(
                    category=CATEGORY,
                    symbol=symbol,
                    buyLeverage=str(leverage),
                    sellLeverage=str(leverage),
                )
            except Exception as e:  # noqa: BLE001
                if "110043" in str(e):  # leverage not modified
                    return None
                raise

        await asyncio.to_thread(_call)

    async def set_isolated(self, symbol: str, leverage: int) -> None:
        if self.dry_run:
            log.info("[DRY] set isolated margin %s x%s", symbol, leverage)
            return

        def _call():
            try:
                return self.http.switch_margin_mode(
                    category=CATEGORY,
                    symbol=symbol,
                    tradeMode=1,  # 1 = isolated
                    buyLeverage=str(leverage),
                    sellLeverage=str(leverage),
                )
            except Exception as e:  # noqa: BLE001
                if "110026" in str(e):  # already in this mode
                    return None
                raise

        await asyncio.to_thread(_call)

    # ---------------- orders ----------------
    async def open_entry(
        self,
        *,
        symbol: str,
        side_buy: bool,
        qty: Decimal,
        price: Optional[Decimal],
        order_link_id: str,
        order_type: str = "Limit",
        reduce_only: bool = False,
    ) -> dict:
        side = "Buy" if side_buy else "Sell"
        params = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": _s(qty),
            "positionIdx": 0,
            "orderLinkId": order_link_id,
            "reduceOnly": reduce_only,
        }
        if order_type == "Limit" and price is not None:
            params["price"] = _s(price)
            params["timeInForce"] = "GTC"
        if self.dry_run:
            log.info("[DRY] open_entry %s", params)
            return {"dry_run": True, "orderLinkId": order_link_id}

        def _call():
            return self.http.place_order(**params)

        return await asyncio.to_thread(_call)

    async def set_stop(self, symbol: str, side_buy: bool, sl_price: Decimal) -> dict:
        """Position-attached stop loss (trading-stop endpoint)."""
        params = {
            "category": CATEGORY,
            "symbol": symbol,
            "stopLoss": _s(sl_price),
            "positionIdx": 0,
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
        }
        if self.dry_run:
            log.info("[DRY] set_stop %s", params)
            return {"dry_run": True}

        def _call():
            return self.http.set_trading_stop(**params)

        return await asyncio.to_thread(_call)

    async def amend_stop(self, symbol: str, sl_price: Decimal) -> dict:
        return await self.set_stop(symbol, True, sl_price)

    async def place_tp_ladder(
        self,
        *,
        symbol: str,
        entry_side_buy: bool,
        total_qty: Decimal,
        tps: list[Decimal],
        qty_step: Decimal,
        order_link_prefix: str,
    ) -> list[dict]:
        """Reduce-only limit orders, qty split equally across TP levels."""
        from .sizing import floor_to_step

        if not tps:
            return []
        n = len(tps)
        per = floor_to_step(total_qty / Decimal(n), qty_step)
        if per <= 0:
            log.warning("TP ladder: per-leg qty rounds to 0 for %s", symbol)
            return []
        close_side = "Sell" if entry_side_buy else "Buy"
        results = []
        allocated = Decimal(0)
        for i, tp in enumerate(tps):
            leg_qty = per
            if i == n - 1:  # last leg takes remainder
                leg_qty = floor_to_step(total_qty - allocated, qty_step)
            if leg_qty <= 0:
                continue
            allocated += leg_qty
            link = f"{order_link_prefix}-tp{i+1}"
            params = {
                "category": CATEGORY,
                "symbol": symbol,
                "side": close_side,
                "orderType": "Limit",
                "qty": _s(leg_qty),
                "price": _s(tp),
                "positionIdx": 0,
                "reduceOnly": True,
                "timeInForce": "GTC",
                "orderLinkId": link,
            }
            if self.dry_run:
                log.info("[DRY] place_tp %s", params)
                results.append({"dry_run": True, "orderLinkId": link})
                continue

            def _call(p=params):
                return self.http.place_order(**p)

            results.append(await asyncio.to_thread(_call))
        return results

    async def partial_close(
        self, *, symbol: str, entry_side_buy: bool, qty: Decimal, order_link_id: str
    ) -> dict:
        close_side = "Sell" if entry_side_buy else "Buy"
        params = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": _s(qty),
            "positionIdx": 0,
            "reduceOnly": True,
            "orderLinkId": order_link_id,
        }
        if self.dry_run:
            log.info("[DRY] partial_close %s", params)
            return {"dry_run": True}

        def _call():
            return self.http.place_order(**params)

        return await asyncio.to_thread(_call)

    async def close_all(self, symbol: str, entry_side_buy: bool, qty: Decimal) -> dict:
        await self.cancel_all(symbol)
        close_side = "Sell" if entry_side_buy else "Buy"
        params = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": _s(qty),
            "positionIdx": 0,
            "reduceOnly": True,
        }
        if self.dry_run:
            log.info("[DRY] close_all %s", params)
            return {"dry_run": True}

        def _call():
            return self.http.place_order(**params)

        return await asyncio.to_thread(_call)

    async def cancel_all(self, symbol: Optional[str] = None) -> dict:
        params = {"category": CATEGORY}
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = "USDT"
        if self.dry_run:
            log.info("[DRY] cancel_all %s", params)
            return {"dry_run": True}

        def _call():
            return self.http.cancel_all_orders(**params)

        return await asyncio.to_thread(_call)

    async def cancel_order(self, symbol: str, order_link_id: str) -> dict:
        if self.dry_run:
            log.info("[DRY] cancel_order %s %s", symbol, order_link_id)
            return {"dry_run": True}

        def _call():
            return self.http.cancel_order(
                category=CATEGORY, symbol=symbol, orderLinkId=order_link_id
            )

        return await asyncio.to_thread(_call)

    async def get_position(self, symbol: str) -> Optional[dict]:
        def _call():
            return self.http.get_positions(category=CATEGORY, symbol=symbol)

        resp = await asyncio.to_thread(_call)
        lst = resp["result"]["list"]
        for p in lst:
            if Decimal(str(p.get("size") or 0)) != 0:
                return p
        return lst[0] if lst else None

    async def get_last_price(self, symbol: str) -> Decimal:
        def _call():
            return self.http.get_tickers(category=CATEGORY, symbol=symbol)

        resp = await asyncio.to_thread(_call)
        return Decimal(str(resp["result"]["list"][0]["lastPrice"]))

    # ---------------- private websocket ----------------
    def start_private_ws(
        self,
        on_order: Callable[[dict], None],
        on_position: Callable[[dict], None],
        on_execution: Callable[[dict], None],
    ) -> None:
        if self.dry_run:
            log.info("[DRY] private WS not started")
            return
        self._ws = WebSocket(
            testnet=self.testnet,
            channel_type="private",
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
        )
        self._ws.order_stream(callback=lambda m: _safe(on_order, m))
        self._ws.position_stream(callback=lambda m: _safe(on_position, m))
        self._ws.execution_stream(callback=lambda m: _safe(on_execution, m))
        log.info("private WS subscribed (order/position/execution)")


def _safe(cb, msg):
    try:
        for row in msg.get("data", []):
            cb(row)
    except Exception:  # noqa: BLE001
        log.exception("ws callback error")


def _s(d: Decimal) -> str:
    return format(d.normalize(), "f")
