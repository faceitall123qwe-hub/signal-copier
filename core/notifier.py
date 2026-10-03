"""Notifier: DMs the operator on every meaningful event.

The actual send coroutine is injected at startup by the control bot so this
module stays free of telegram imports and circular dependencies.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("notifier")

_send: Optional[Callable[[str], Awaitable[None]]] = None


def bind(send_coro: Callable[[str], Awaitable[None]]) -> None:
    global _send
    _send = send_coro


async def notify(text: str) -> None:
    log.info("NOTIFY: %s", text.replace("\n", " | "))
    if _send is None:
        return
    try:
        await _send(text)
    except Exception:  # noqa: BLE001
        log.exception("failed to send notification")


def _scope(channel_id, topic_id) -> str:
    if topic_id:
        return f"chan {channel_id} / topic {topic_id}"
    return f"chan {channel_id}"


async def entry(channel_id, topic_id, symbol, side, qty, entry_price, sl, tps, excerpt):
    await notify(
        f"🟢 ENTRY [{_scope(channel_id, topic_id)}]\n"
        f"{symbol} {side} qty={qty} @ {entry_price}\n"
        f"SL={sl} TP={tps}\n"
        f"src: {excerpt[:160]}"
    )


async def sltp_set(symbol, sl, tps):
    await notify(f"🎯 SL+TP set {symbol}: SL={sl} TP={tps}")


async def fill(symbol, detail):
    await notify(f"✅ FILL {symbol}: {detail}")


async def partial(symbol, pct, detail=""):
    await notify(f"✂️ PARTIAL CLOSE {symbol} {pct}% {detail}")


async def closed(symbol, pnl, reason=""):
    await notify(f"🔚 CLOSED {symbol} realized_pnl={pnl} {reason}")


async def skip(channel_id, topic_id, symbol, reason):
    await notify(f"⏭️ SKIP [{_scope(channel_id, topic_id)}] {symbol}: {reason}")


async def error(context, exc):
    await notify(f"❗ ERROR {context}: {exc}")


async def risk_halt(reason):
    await notify(f"🛑 RISK HALT: {reason}")


async def confirm_needed(signal_id, channel_id, topic_id, parsed):
    await notify(
        f"❓ LOW CONFIDENCE [{_scope(channel_id, topic_id)}] id={signal_id}\n"
        f"intent={parsed.intent.value} {parsed.symbol} {parsed.side} "
        f"conf={parsed.confidence:.2f}\n"
        f"/confirm {signal_id}  or  /reject {signal_id}\n"
        f"src: {parsed.raw[:200]}"
    )
