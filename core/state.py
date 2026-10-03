"""Trade state helpers: status constants, idempotent order-link derivation,
and scoped management-message matching."""
from __future__ import annotations

import hashlib
from typing import Optional

from storage.db import db

# statuses
PENDING_ENTRY = "PENDING_ENTRY"
OPEN = "OPEN"
PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
CLOSED = "CLOSED"
CANCELLED = "CANCELLED"

ACTIVE = (PENDING_ENTRY, OPEN, PARTIALLY_CLOSED)


def order_link_id(channel_id: int, message_id: int) -> str:
    """Deterministic, idempotent id from (source_channel_id, message_id).

    Bybit orderLinkId max length is 36 chars; we hash to stay safe.
    """
    h = hashlib.sha1(f"{channel_id}:{message_id}".encode()).hexdigest()[:20]
    return f"sc-{h}"


async def match_management_trade(
    *, channel_id: int, topic_id: Optional[int], symbol: Optional[str]
) -> tuple[Optional[dict], str]:
    """Scope a management message to a trade.

    1. most-recent active trade from the SAME (channel, topic);
    2. else, if symbol uniquely identifies one active trade anywhere, use it;
    3. else ambiguous -> (None, reason) so caller asks for /confirm.

    Never matches across sub-channels implicitly.
    """
    scoped = await db.latest_open_for_scope(channel_id, topic_id)
    if scoped is not None:
        if symbol and scoped["symbol"] != symbol:
            # message names a different symbol than this scope's open trade
            by_symbol = await db.open_trades_for_symbol(symbol)
            same_scope = [
                t for t in by_symbol
                if t["source_channel_id"] == channel_id and t["topic_id"] == topic_id
            ]
            if len(same_scope) == 1:
                return same_scope[0], "scoped+symbol"
            return None, "symbol mismatch within scope; ambiguous"
        return scoped, "scoped"

    if symbol:
        by_symbol = await db.open_trades_for_symbol(symbol)
        if len(by_symbol) == 1:
            return by_symbol[0], "unique-symbol"
        if len(by_symbol) > 1:
            return None, "multiple open trades for symbol; ambiguous"

    return None, "no open trade in scope"
