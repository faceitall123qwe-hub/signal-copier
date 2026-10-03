"""In-memory exchange with the same async surface as BybitClient.

Lets the full pipeline (parser -> risk -> sizing -> executor -> state machine)
run with no API keys. Orders are recorded, positions are tracked, and fills
queue the same rows the Bybit private WS would; deliver() feeds them to the
executor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .sizing import SymbolFilters, floor_to_step

DEFAULT_FILTERS = SymbolFilters(
    qty_step=Decimal("0.001"),
    min_order_qty=Decimal("0.001"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.1"),
    max_leverage=100,
    maintenance_margin_rate=Decimal("0.005"),
)


@dataclass
class MockOrder:
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    price: Optional[Decimal]
    order_link_id: str
    reduce_only: bool
    status: str = "New"


@dataclass
class MockPosition:
    symbol: str
    side: str
    size: Decimal
    avg_price: Decimal
    stop_loss: Optional[Decimal] = None
    realised_pnl: Decimal = Decimal(0)


@dataclass
class MockExchange:
    equity: Decimal = Decimal("10000")
    prices: dict[str, Decimal] = field(default_factory=dict)
    filters: dict[str, SymbolFilters] = field(default_factory=dict)
    orders: list[MockOrder] = field(default_factory=list)
    positions: dict[str, MockPosition] = field(default_factory=dict)
    leverage: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    dry_run: bool = False
    testnet: bool = True

    ws_events: list[tuple[str, dict]] = field(default_factory=list)

    def _log(self, name: str, **kw) -> None:
        self.calls.append((name, kw))

    # ---------------- account ----------------
    async def get_equity(self) -> Decimal:
        return self.equity

    async def get_available_margin(self) -> Decimal:
        return self.equity

    async def get_filters(self, symbol: str) -> SymbolFilters:
        return self.filters.get(symbol, DEFAULT_FILTERS)

    async def get_last_price(self, symbol: str) -> Decimal:
        if symbol not in self.prices:
            raise ValueError(f"no mock price for {symbol}")
        return self.prices[symbol]

    async def get_position(self, symbol: str) -> Optional[dict]:
        p = self.positions.get(symbol)
        if p is None:
            return {"symbol": symbol, "size": "0"}
        return {"symbol": symbol, "side": p.side, "size": str(p.size),
                "avgPrice": str(p.avg_price), "stopLoss": str(p.stop_loss or "")}

    # ---------------- leverage & margin ----------------
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage[symbol] = leverage
        self._log("set_leverage", symbol=symbol, leverage=leverage)

    async def set_isolated(self, symbol: str, leverage: int) -> None:
        self._log("set_isolated", symbol=symbol, leverage=leverage)

    # ---------------- orders ----------------
    async def open_entry(self, *, symbol, side_buy, qty, price, order_link_id,
                         order_type="Limit", reduce_only=False) -> dict:
        o = MockOrder(symbol, "Buy" if side_buy else "Sell", order_type, qty,
                      price, order_link_id, reduce_only)
        self.orders.append(o)
        self._log("open_entry", symbol=symbol, qty=qty, price=price,
                  order_type=order_type, link=order_link_id)
        if order_type == "Market":
            self._fill(o, self.prices.get(symbol, price or Decimal(0)))
        return {"orderLinkId": order_link_id}

    async def set_stop(self, symbol, side_buy, sl_price) -> dict:
        if symbol in self.positions:
            self.positions[symbol].stop_loss = sl_price
        self._log("set_stop", symbol=symbol, sl=sl_price)
        return {}

    async def amend_stop(self, symbol, sl_price) -> dict:
        return await self.set_stop(symbol, True, sl_price)

    async def place_tp_ladder(self, *, symbol, entry_side_buy, total_qty, tps,
                              qty_step, order_link_prefix) -> list[dict]:
        if not tps:
            return []
        n = len(tps)
        per = floor_to_step(total_qty / Decimal(n), qty_step)
        close_side = "Sell" if entry_side_buy else "Buy"
        out, allocated = [], Decimal(0)
        for i, tp in enumerate(tps):
            leg = per if i < n - 1 else floor_to_step(total_qty - allocated, qty_step)
            if leg <= 0:
                continue
            allocated += leg
            link = f"{order_link_prefix}-tp{i+1}"
            self.orders.append(MockOrder(symbol, close_side, "Limit", leg, tp, link, True))
            out.append({"orderLinkId": link})
        self._log("place_tp_ladder", symbol=symbol, tps=tps, qty=total_qty)
        return out

    async def partial_close(self, *, symbol, entry_side_buy, qty, order_link_id) -> dict:
        self._log("partial_close", symbol=symbol, qty=qty)
        self._reduce(symbol, qty, self.prices.get(symbol))
        return {}

    async def close_all(self, symbol, entry_side_buy, qty) -> dict:
        await self.cancel_all(symbol)
        self._log("close_all", symbol=symbol, qty=qty)
        p = self.positions.get(symbol)
        if p:
            self._reduce(symbol, p.size, self.prices.get(symbol))
        return {}

    async def cancel_all(self, symbol: Optional[str] = None) -> dict:
        for o in self.orders:
            if o.status == "New" and (symbol is None or o.symbol == symbol):
                o.status = "Cancelled"
        self._log("cancel_all", symbol=symbol)
        return {}

    async def cancel_order(self, symbol, order_link_id) -> dict:
        for o in self.orders:
            if o.order_link_id == order_link_id and o.status == "New":
                o.status = "Cancelled"
        self._log("cancel_order", symbol=symbol, link=order_link_id)
        return {}

    # ---------------- simulated private WS ----------------
    def start_private_ws(self, on_order, on_position, on_execution) -> None:
        pass

    async def deliver(self, executor) -> None:
        while self.ws_events:
            kind, row = self.ws_events.pop(0)
            if kind == "order":
                await executor._handle_order(row)
            else:
                await executor._handle_position(row)

    def open_orders(self, symbol: Optional[str] = None) -> list[MockOrder]:
        return [o for o in self.orders
                if o.status == "New" and (symbol is None or o.symbol == symbol)]

    def simulate_fill(self, order_link_id: str) -> dict:
        """Fill a resting order at its limit price; returns the WS order row."""
        o = next(o for o in self.orders
                 if o.order_link_id == order_link_id and o.status == "New")
        return self._fill(o, o.price)

    def _fill(self, o: MockOrder, price: Decimal) -> dict:
        o.status = "Filled"
        row = {"symbol": o.symbol, "orderLinkId": o.order_link_id,
               "orderStatus": "Filled", "reduceOnly": o.reduce_only,
               "avgPrice": str(price)}
        self.ws_events.append(("order", row))
        if o.reduce_only:
            self._reduce(o.symbol, o.qty, price)
        else:
            side = "long" if o.side == "Buy" else "short"
            p = self.positions.get(o.symbol)
            if p is None:
                self.positions[o.symbol] = MockPosition(o.symbol, side, o.qty, price)
            else:
                p.avg_price = (p.avg_price * p.size + price * o.qty) / (p.size + o.qty)
                p.size += o.qty
        return row

    def _reduce(self, symbol: str, qty: Decimal, price: Optional[Decimal]) -> None:
        p = self.positions.get(symbol)
        if p is None:
            return
        qty = min(qty, p.size)
        if price is not None:
            sign = 1 if p.side == "long" else -1
            pnl = (price - p.avg_price) * qty * sign
            p.realised_pnl += pnl
            self.equity += pnl
        p.size -= qty
        if p.size == 0:
            del self.positions[symbol]
            self.ws_events.append(("position", {"symbol": symbol, "size": "0",
                                                "curRealisedPnl": str(p.realised_pnl)}))
