"""End-to-end executor tests against MockExchange (no network, no keys)."""
import asyncio
from decimal import Decimal

import pytest

import core.executor as ex_mod
import core.risk_guard as rg_mod
import core.state as state_mod
from core import state
from core.executor import Executor
from exchange.mock_exchange import MockExchange
from parser import regex_fastpath as fp
from storage.db import DB

CHAN = -100111


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = DB(str(tmp_path / "t.sqlite"))
    for mod in (ex_mod, rg_mod, state_mod):
        monkeypatch.setattr(mod, "db", d)
    rg_mod.guard.resume()
    mock = MockExchange(prices={"BTCUSDT": Decimal("64000")})
    return d, mock, Executor(mock)


def run(d, coro):
    async def wrap():
        await d.connect()
        try:
            await coro
        finally:
            await d.close()
    asyncio.run(wrap())


async def _signal(executor, text, msg_id, conf=None):
    parsed = fp.parse(text)
    assert parsed is not None, text
    if conf is not None:
        parsed.confidence = conf
    await executor.handle_parsed(parsed, channel_id=CHAN, topic_id=None, message_id=msg_id)


ENTRY = "LONG BTCUSDT Entry: 64000 SL: 63000 TP1: 65000 TP2: 66000"


def test_limit_entry_fill_places_sl_and_tp_ladder(env):
    d, mock, executor = env

    async def go():
        await _signal(executor, ENTRY, 1)
        trade = (await d.open_trades())[0]
        assert trade["status"] == state.PENDING_ENTRY
        # risk 1% of 10k = 100 USDT over a 1000 stop -> 0.1 BTC
        assert Decimal(str(trade["qty"])) == Decimal("0.1")

        mock.simulate_fill(trade["order_link_id"])
        await mock.deliver(executor)

        trade = await d.get_trade(trade["id"])
        assert trade["status"] == state.OPEN
        assert mock.positions["BTCUSDT"].stop_loss == Decimal("63000")
        legs = mock.open_orders("BTCUSDT")
        assert [o.price for o in legs] == [Decimal("65000"), Decimal("66000")]
        assert sum(o.qty for o in legs) == Decimal("0.1")

    run(d, go())


def test_tp1_moves_sl_to_breakeven_and_tp2_closes(env):
    d, mock, executor = env

    async def go():
        await _signal(executor, ENTRY, 1)
        trade = (await d.open_trades())[0]
        link = trade["order_link_id"]
        mock.simulate_fill(link)
        await mock.deliver(executor)

        mock.simulate_fill(f"{link}-tp1")
        await mock.deliver(executor)
        trade = await d.get_trade(trade["id"])
        assert trade["status"] == state.PARTIALLY_CLOSED
        assert mock.positions["BTCUSDT"].stop_loss == Decimal("64000")

        mock.simulate_fill(f"{link}-tp2")
        await mock.deliver(executor)
        trade = await d.get_trade(trade["id"])
        assert trade["status"] == state.CLOSED
        # 0.05 * 1000 + 0.05 * 2000
        assert trade["realized_pnl"] == pytest.approx(150.0)
        assert mock.equity == Decimal("10150")

    run(d, go())


def test_duplicate_message_does_not_double_fire(env):
    d, mock, executor = env

    async def go():
        await _signal(executor, ENTRY, 7)
        await _signal(executor, ENTRY, 7)
        assert len(await d.open_trades()) == 1
        assert sum(1 for c, _ in mock.calls if c == "open_entry") == 1

    run(d, go())


def test_panic_blocks_new_entries(env):
    d, mock, executor = env

    async def go():
        rg_mod.guard.panic()
        await _signal(executor, ENTRY, 1)
        assert await d.open_trades() == []
        assert not any(c == "open_entry" for c, _ in mock.calls)

    run(d, go())


def test_low_confidence_waits_for_confirm(env):
    d, mock, executor = env

    async def go():
        await _signal(executor, ENTRY, 1, conf=0.5)
        assert await d.open_trades() == []
        assert await executor.confirm(1) is True
        assert len(await d.open_trades()) == 1

    run(d, go())


def test_full_close_cancels_unfilled_limit(env):
    d, mock, executor = env

    async def go():
        await _signal(executor, ENTRY, 1)
        await _signal(executor, "Close BTC position now", 2)
        trade = (await d.get_trade(1))
        assert trade["status"] == state.CANCELLED
        assert mock.open_orders() == []

    run(d, go())


def test_market_full_close_realises_pnl(env):
    d, mock, executor = env

    async def go():
        await _signal(executor, ENTRY, 1)
        trade = (await d.open_trades())[0]
        mock.simulate_fill(trade["order_link_id"])
        await mock.deliver(executor)

        mock.prices["BTCUSDT"] = Decimal("63500")
        await _signal(executor, "Close BTC position now", 2)
        await mock.deliver(executor)
        trade = await d.get_trade(trade["id"])
        assert trade["status"] == state.CLOSED
        assert "BTCUSDT" not in mock.positions
        assert mock.equity == Decimal("9950")

    run(d, go())
