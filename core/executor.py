"""Routing + execution: turns ParsedSignal into Bybit actions and drives the
trade state machine. WebSocket fill events feed back here."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from config import settings
from exchange.bybit_client import BybitClient
from exchange.sizing import compute_size, round_price
from parser.schema import Intent, ParsedSignal, Side
from storage.db import db

from . import notifier, state
from .risk_guard import guard

log = logging.getLogger("executor")


class Executor:
    def __init__(self, bybit: BybitClient) -> None:
        self.bybit = bybit
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # pending low-confidence signals awaiting /confirm
        self._pending: dict[int, dict] = {}
        self._pending_seq = 0
        # order_link_id -> trade_id  (entry orders we are awaiting fills for)
        self._entry_links: dict[str, int] = {}

    def attach_loop(self) -> None:
        self.loop = asyncio.get_running_loop()

    # ============== entrypoint from listener ==============
    async def handle_parsed(
        self,
        parsed: ParsedSignal,
        *,
        channel_id: int,
        topic_id: Optional[int],
        message_id: int,
    ) -> None:
        if parsed.intent in (Intent.NOISE, Intent.INFO):
            log.debug("ignoring %s", parsed.intent)
            return

        if parsed.confidence < settings.min_confidence:
            self._pending_seq += 1
            sid = self._pending_seq
            self._pending[sid] = dict(
                parsed=parsed, channel_id=channel_id, topic_id=topic_id,
                message_id=message_id,
            )
            await notifier.confirm_needed(sid, channel_id, topic_id, parsed)
            return

        await self._route(parsed, channel_id, topic_id, message_id)

    async def confirm(self, signal_id: int) -> bool:
        item = self._pending.pop(signal_id, None)
        if not item:
            return False
        await self._route(
            item["parsed"], item["channel_id"], item["topic_id"], item["message_id"]
        )
        return True

    def reject(self, signal_id: int) -> bool:
        return self._pending.pop(signal_id, None) is not None

    # ============== routing ==============
    async def _route(self, parsed, channel_id, topic_id, message_id):
        try:
            if parsed.intent == Intent.NEW_ENTRY:
                await self._new_entry(parsed, channel_id, topic_id, message_id)
            elif parsed.intent == Intent.MODIFY_SL:
                await self._modify_sl(parsed, channel_id, topic_id)
            elif parsed.intent == Intent.ADD_REPLACE_TP:
                await self._replace_tp(parsed, channel_id, topic_id)
            elif parsed.intent == Intent.PARTIAL_CLOSE:
                await self._close(parsed, channel_id, topic_id, full=False)
            elif parsed.intent == Intent.FULL_CLOSE:
                await self._close(parsed, channel_id, topic_id, full=True)
        except Exception as e:  # noqa: BLE001
            log.exception("route error")
            await notifier.error(f"route {parsed.intent}", e)

    # ============== NEW_ENTRY ==============
    async def _new_entry(self, parsed: ParsedSignal, channel_id, topic_id, message_id):
        symbol = parsed.symbol
        if not symbol or parsed.side is None:
            await notifier.skip(channel_id, topic_id, symbol, "incomplete entry signal")
            return
        # no entry price => market entry (e.g. "Вход: рынок")
        market_only = not parsed.entries

        link = state.order_link_id(channel_id, message_id)
        if await db.trade_exists_for_link(link):
            log.info("duplicate entry (link %s) — skipping", link)
            return

        equity = await self.bybit.get_equity()
        ok, reason = await guard.can_open_new(float(equity))
        if not ok:
            await notifier.skip(channel_id, topic_id, symbol, reason)
            return

        # symbol collision (one-way mode nets per symbol)
        existing = await db.open_trades_for_symbol(symbol)
        if existing:
            policy = settings.on_symbol_collision
            if policy == "skip":
                await notifier.skip(channel_id, topic_id, symbol,
                                    "symbol collision (open position exists)")
                return
            elif policy == "allow_same_side":
                if any(t["side"] != parsed.side.value for t in existing):
                    await notifier.skip(channel_id, topic_id, symbol,
                                        "collision: opposite side already open")
                    return
                # same side: proceed (adds to position)
            elif policy == "subaccount":
                await notifier.skip(channel_id, topic_id, symbol,
                                    "collision: subaccount routing not configured")
                return

        filters = await self.bybit.get_filters(symbol)
        if market_only:
            entry_price = round_price(
                await self.bybit.get_last_price(symbol), filters.tick_size
            )
        else:
            entry_price = round_price(Decimal(str(parsed.entries[0])), filters.tick_size)
        sl = parsed.stop_loss
        avail = await self.bybit.get_available_margin()

        sizing = compute_size(
            equity=equity,
            entry=entry_price,
            sl=sl,
            filters=filters,
            risk_pct=settings.risk_pct,
            signal_leverage=parsed.leverage,
            default_leverage=settings.default_leverage,
            max_leverage=settings.max_leverage,
            fee_buffer=settings.fee_buffer,
            fallback_stop_pct=settings.fallback_stop_pct,
            available_margin=avail,
            on_insufficient_margin=settings.on_insufficient_margin,
        )
        if not sizing.ok:
            await notifier.skip(channel_id, topic_id, symbol, sizing.reason)
            return

        sl_price = (
            round_price(Decimal(str(sl)), filters.tick_size) if sl is not None
            else round_price(entry_price * (Decimal(1) - Decimal(str(settings.fallback_stop_pct or 0))), filters.tick_size)
        )
        tps = [round_price(Decimal(str(t)), filters.tick_size) for t in parsed.take_profits]
        side_buy = parsed.side == Side.LONG

        await self.bybit.set_isolated(symbol, sizing.leverage)
        await self.bybit.set_leverage(symbol, sizing.leverage)

        # market-only signals ignore ENTRY_ORDER_TYPE and go in at market
        if market_only:
            order_type = "Market"
        else:
            order_type = "Limit" if settings.entry_order_type == "limit" else "Market"
        price_arg = entry_price if order_type == "Limit" else None

        trade_id = await db.create_trade(
            source_channel_id=channel_id, topic_id=topic_id, source_msg_id=message_id,
            symbol=symbol, side=parsed.side.value, status=state.PENDING_ENTRY,
            entry_price=float(entry_price), sl=float(sl_price),
            tps=[float(t) for t in tps], qty=float(sizing.qty),
            leverage=sizing.leverage, order_link_id=link,
            exchange_order_ids=[link],
        )
        self._entry_links[link] = trade_id

        await self.bybit.open_entry(
            symbol=symbol, side_buy=side_buy, qty=sizing.qty, price=price_arg,
            order_link_id=link, order_type=order_type,
        )
        await notifier.entry(
            channel_id, topic_id, symbol, parsed.side.value, sizing.qty,
            entry_price, sl_price, [float(t) for t in tps], parsed.raw,
        )

        if self.bybit.dry_run or order_type == "Market":
            # no live WS fills to wait for (dry run) or fills immediately (market)
            await self._on_entry_filled(trade_id)
        else:
            self._schedule_entry_timeout(trade_id, link, symbol)

    async def _on_entry_filled(self, trade_id: int):
        trade = await db.get_trade(trade_id)
        if not trade or trade["status"] not in (state.PENDING_ENTRY,):
            return
        symbol = trade["symbol"]
        side_buy = trade["side"] == "long"
        filters = await self.bybit.get_filters(symbol)
        sl_price = Decimal(str(trade["sl"]))
        import json
        tps = [Decimal(str(t)) for t in json.loads(trade["tps_json"] or "[]")]

        await self.bybit.set_stop(symbol, side_buy, sl_price)
        if tps:
            await self.bybit.place_tp_ladder(
                symbol=symbol, entry_side_buy=side_buy,
                total_qty=Decimal(str(trade["qty"])), tps=tps,
                qty_step=filters.qty_step,
                order_link_prefix=trade["order_link_id"],
            )
        await db.update_trade(trade_id, status=state.OPEN)
        await notifier.sltp_set(symbol, float(sl_price), [float(t) for t in tps])

    def _schedule_entry_timeout(self, trade_id, link, symbol):
        async def _timeout():
            await asyncio.sleep(settings.entry_fill_timeout_min * 60)
            trade = await db.get_trade(trade_id)
            if trade and trade["status"] == state.PENDING_ENTRY:
                await self.bybit.cancel_order(symbol, link)
                await db.update_trade(trade_id, status=state.CANCELLED,
                                      closed_at=_now())
                self._entry_links.pop(link, None)
                await notifier.skip(trade["source_channel_id"], trade["topic_id"],
                                    symbol, "entry fill timeout — cancelled")
        asyncio.create_task(_timeout())

    # ============== MODIFY_SL ==============
    async def _modify_sl(self, parsed: ParsedSignal, channel_id, topic_id):
        trade, reason = await state.match_management_trade(
            channel_id=channel_id, topic_id=topic_id, symbol=parsed.symbol
        )
        if not trade:
            await notifier.skip(channel_id, topic_id, parsed.symbol,
                                f"MODIFY_SL unmatched: {reason}")
            return
        symbol = trade["symbol"]
        filters = await self.bybit.get_filters(symbol)
        if parsed.sl_to_breakeven:
            new_sl = Decimal(str(trade["entry_price"]))
        elif parsed.stop_loss is not None:
            new_sl = round_price(Decimal(str(parsed.stop_loss)), filters.tick_size)
        else:
            await notifier.skip(channel_id, topic_id, symbol, "MODIFY_SL no price/BE")
            return
        await self.bybit.amend_stop(symbol, new_sl)
        await db.update_trade(trade["id"], sl=float(new_sl))
        await notifier.notify(f"🔧 SL updated {symbol} -> {new_sl} ({reason})")

    # ============== ADD_REPLACE_TP ==============
    async def _replace_tp(self, parsed: ParsedSignal, channel_id, topic_id):
        trade, reason = await state.match_management_trade(
            channel_id=channel_id, topic_id=topic_id, symbol=parsed.symbol
        )
        if not trade:
            await notifier.skip(channel_id, topic_id, parsed.symbol,
                                f"ADD_REPLACE_TP unmatched: {reason}")
            return
        if not parsed.take_profits:
            await notifier.skip(channel_id, topic_id, trade["symbol"],
                                "ADD_REPLACE_TP no targets")
            return
        symbol = trade["symbol"]
        side_buy = trade["side"] == "long"
        filters = await self.bybit.get_filters(symbol)
        await self.bybit.cancel_all(symbol)  # drop existing reduce-only TP legs
        # reattach SL (cancel_all may remove conditional orders) then re-ladder
        await self.bybit.set_stop(symbol, side_buy, Decimal(str(trade["sl"])))
        tps = [round_price(Decimal(str(t)), filters.tick_size) for t in parsed.take_profits]
        pos = await self.bybit.get_position(symbol)
        qty = Decimal(str(pos["size"])) if pos and pos.get("size") else Decimal(str(trade["qty"]))
        await self.bybit.place_tp_ladder(
            symbol=symbol, entry_side_buy=side_buy, total_qty=qty, tps=tps,
            qty_step=filters.qty_step, order_link_prefix=trade["order_link_id"] + "-r",
        )
        await db.update_trade(trade["id"], tps=[float(t) for t in tps])
        await notifier.notify(f"🎯 TP replaced {symbol}: {[float(t) for t in tps]} ({reason})")

    # ============== PARTIAL / FULL CLOSE ==============
    async def _close(self, parsed: ParsedSignal, channel_id, topic_id, *, full: bool):
        trade, reason = await state.match_management_trade(
            channel_id=channel_id, topic_id=topic_id, symbol=parsed.symbol
        )
        if not trade:
            await notifier.skip(channel_id, topic_id, parsed.symbol,
                                f"CLOSE unmatched: {reason}")
            return
        symbol = trade["symbol"]
        side_buy = trade["side"] == "long"
        filters = await self.bybit.get_filters(symbol)
        pos = await self.bybit.get_position(symbol)
        size = Decimal(str(pos["size"])) if pos and pos.get("size") else Decimal(str(trade["qty"]))

        if full:
            # unfilled limit entry ("Отменяю лимитку") -> cancel, don't trade
            if trade["status"] == state.PENDING_ENTRY:
                await self.bybit.cancel_order(symbol, trade["order_link_id"])
                self._entry_links.pop(trade["order_link_id"], None)
                await db.update_trade(trade["id"], status=state.CANCELLED,
                                      closed_at=_now())
                await notifier.notify(
                    f"🚫 cancelled unfilled entry {symbol} ({reason})"
                )
                return
            await self.bybit.close_all(symbol, side_buy, size)
            await db.update_trade(trade["id"], status=state.CLOSED, closed_at=_now())
            await notifier.closed(symbol, "(market close)", f"full close ({reason})")
        else:
            pct = Decimal(str(parsed.close_pct or 50)) / Decimal(100)
            from exchange.sizing import floor_to_step
            qty = floor_to_step(size * pct, filters.qty_step)
            if qty <= 0:
                await notifier.skip(channel_id, topic_id, symbol,
                                    "partial close qty rounds to 0")
                return
            link = f"{trade['order_link_id']}-pc{int(parsed.close_pct or 50)}"
            await self.bybit.partial_close(
                symbol=symbol, entry_side_buy=side_buy, qty=qty, order_link_id=link
            )
            await db.update_trade(trade["id"], status=state.PARTIALLY_CLOSED)
            await notifier.partial(symbol, float(parsed.close_pct or 50),
                                   f"qty={qty} ({reason})")

    # ============== WebSocket fill handlers (called from WS thread) ==============
    def on_order(self, row: dict):
        self._dispatch(self._handle_order(row))

    def on_position(self, row: dict):
        self._dispatch(self._handle_position(row))

    def on_execution(self, row: dict):
        # executions are summarised via order/position streams; kept for logging
        log.debug("execution: %s", row.get("symbol"))

    def _dispatch(self, coro):
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _handle_order(self, row: dict):
        link = row.get("orderLinkId") or ""
        status = row.get("orderStatus")
        reduce_only = row.get("reduceOnly")
        if status != "Filled":
            return
        # entry fill
        if link in self._entry_links and not reduce_only:
            trade_id = self._entry_links.pop(link)
            await self._on_entry_filled(trade_id)
            await notifier.fill(row.get("symbol"), f"entry filled {row.get('avgPrice')}")
            return
        # TP leg fill -> partial; move SL to BE after first TP
        if reduce_only and "-tp" in link:
            base = link.rsplit("-tp", 1)[0].replace("-r", "")
            trade = await db.get_trade_by_link(base)
            if not trade:
                return
            await db.update_trade(trade["id"], status=state.PARTIALLY_CLOSED)
            await notifier.partial(trade["symbol"], "TP",
                                   f"leg filled @ {row.get('avgPrice')}")
            if settings.move_sl_to_be_after_tp1 and link.endswith("-tp1"):
                await self.bybit.amend_stop(trade["symbol"],
                                            Decimal(str(trade["entry_price"])))
                await db.update_trade(trade["id"], sl=trade["entry_price"])
                await notifier.notify(
                    f"🟰 SL -> breakeven {trade['symbol']} after TP1"
                )

    async def _handle_position(self, row: dict):
        symbol = row.get("symbol")
        size = Decimal(str(row.get("size") or 0))
        if size != 0:
            return
        # position closed: settle the most-recent active trade for this symbol
        trades = await db.open_trades_for_symbol(symbol)
        if not trades:
            return
        trade = trades[0]
        pnl = row.get("curRealisedPnl") or row.get("cumRealisedPnl") or 0
        await db.update_trade(trade["id"], status=state.CLOSED, closed_at=_now(),
                              realized_pnl=float(pnl))
        await db.log_pnl(trade["source_channel_id"], symbol, float(pnl), "position closed")
        await notifier.closed(symbol, pnl)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
