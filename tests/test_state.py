import asyncio
import os
import tempfile

from core import state
from storage.db import DB
import storage.db as dbmod


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    d = DB(path)
    # point the module-global db (used by core.state) at this instance
    dbmod.db = d
    state.db = d
    return d, path


def test_order_link_id_deterministic():
    a = state.order_link_id(-1001234567, 42)
    b = state.order_link_id(-1001234567, 42)
    c = state.order_link_id(-1001234567, 43)
    d = state.order_link_id(-1009999999, 42)
    assert a == b
    assert a != c and a != d
    assert len(a) <= 36


def test_dedup_and_scoped_matching():
    async def run():
        d, path = _fresh_db()
        await d.connect()
        try:
            # two sub-channels, each with their own open trade
            await d.create_trade(
                source_channel_id=-100111, topic_id=None, source_msg_id=1,
                symbol="BTCUSDT", side="long", status=state.OPEN,
                entry_price=100, sl=98, tps=[110], qty=1, leverage=10,
                order_link_id="link-A",
            )
            await d.create_trade(
                source_channel_id=-100222, topic_id=None, source_msg_id=2,
                symbol="ETHUSDT", side="short", status=state.OPEN,
                entry_price=50, sl=55, tps=[40], qty=2, leverage=10,
                order_link_id="link-B",
            )

            # a "close" from chan A matches only A's BTC trade
            t, reason = await state.match_management_trade(
                channel_id=-100111, topic_id=None, symbol=None
            )
            assert t is not None and t["symbol"] == "BTCUSDT"

            # chan B management never touches A's trade
            t2, _ = await state.match_management_trade(
                channel_id=-100222, topic_id=None, symbol=None
            )
            assert t2["symbol"] == "ETHUSDT"

            # dedup
            assert not await d.is_processed(-100111, 1)
            await d.mark_processed(-100111, 1)
            assert await d.is_processed(-100111, 1)
        finally:
            await d.close()
            os.unlink(path)

    asyncio.run(run())


def test_unique_symbol_fallback_and_ambiguity():
    async def run():
        d, path = _fresh_db()
        await d.connect()
        try:
            await d.create_trade(
                source_channel_id=-100111, topic_id=5, source_msg_id=1,
                symbol="SOLUSDT", side="long", status=state.OPEN,
                entry_price=100, sl=98, tps=[110], qty=1, leverage=10,
                order_link_id="link-S",
            )
            # different scope, but symbol uniquely identifies the trade
            t, reason = await state.match_management_trade(
                channel_id=-100999, topic_id=None, symbol="SOLUSDT"
            )
            assert t is not None and reason == "unique-symbol"

            # add a second SOL trade -> now ambiguous across scopes
            await d.create_trade(
                source_channel_id=-100222, topic_id=None, source_msg_id=2,
                symbol="SOLUSDT", side="short", status=state.OPEN,
                entry_price=100, sl=102, tps=[90], qty=1, leverage=10,
                order_link_id="link-S2",
            )
            t2, reason2 = await state.match_management_trade(
                channel_id=-100999, topic_id=None, symbol="SOLUSDT"
            )
            assert t2 is None and "ambiguous" in reason2
        finally:
            await d.close()
            os.unlink(path)

    asyncio.run(run())


def test_symbol_collision_detection():
    async def run():
        d, path = _fresh_db()
        await d.connect()
        try:
            await d.create_trade(
                source_channel_id=-100111, topic_id=None, source_msg_id=1,
                symbol="BTCUSDT", side="long", status=state.OPEN,
                entry_price=100, sl=98, tps=[110], qty=1, leverage=10,
                order_link_id="link-X",
            )
            existing = await d.open_trades_for_symbol("BTCUSDT")
            assert len(existing) == 1
            assert await d.count_active_positions() == 1
        finally:
            await d.close()
            os.unlink(path)

    asyncio.run(run())
