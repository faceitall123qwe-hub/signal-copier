"""Keyless demo: scripted signals -> parser -> risk -> sizing -> executor -> MockExchange.

    python demo.py

No Telegram, Bybit or Anthropic credentials needed. Only the regex fast-path
parser is used; messages it can't handle are reported as "would go to LLM".
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from decimal import Decimal

for k, v in {
    "TELEGRAM_API_ID": "0", "TELEGRAM_API_HASH": "demo", "CONTROL_BOT_TOKEN": "demo",
    "ALLOWED_USER_ID": "0", "BYBIT_API_KEY": "demo", "BYBIT_API_SECRET": "demo",
    "ANTHROPIC_API_KEY": "demo", "MODE": "DRY_RUN",
}.items():
    os.environ.setdefault(k, v)

import core.executor as ex_mod  # noqa: E402
import core.risk_guard as rg_mod  # noqa: E402
import core.state as state_mod  # noqa: E402
from core.executor import Executor  # noqa: E402
from exchange.mock_exchange import MockExchange  # noqa: E402
from parser import regex_fastpath  # noqa: E402
from storage.db import DB  # noqa: E402

CHAN = -100000000001

SCRIPT = [
    ("signal", "LONG BTCUSDT Entry: 64000 SL: 63000 TP1: 65000 TP2: 66000"),
    ("fill", "entry"),
    ("fill", "tp1"),
    ("signal", "SHORT ETHUSDT Entry: 3200 SL: 3300 TP: 3000"),
    ("signal", "LONG BTCUSDT Entry: 64000 SL: 63000 TP1: 65000 TP2: 66000"),
    ("signal", "thinking about going long on bitcoin soon maybe"),
    ("price", ("BTCUSDT", "65500")),
    ("signal", "Close BTC position now"),
    ("panic", None),
    ("signal", "LONG SOLUSDT Entry: 150 SL: 145 TP: 165"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s", stream=sys.stdout)
    for noisy in ("aiosqlite", "parser"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    db = DB(os.path.join(tempfile.mkdtemp(), "demo.sqlite"))
    for mod in (ex_mod, rg_mod, state_mod):
        mod.db = db
    await db.connect()

    mock = MockExchange(prices={"BTCUSDT": Decimal("64000"),
                                "ETHUSDT": Decimal("3200"),
                                "SOLUSDT": Decimal("150")})
    executor = Executor(mock)
    msg_id = 0
    btc_link = None

    for action, arg in SCRIPT:
        if action == "signal":
            msg_id += 1
            print(f"\n>>> message #{msg_id}: {arg}")
            parsed = regex_fastpath.parse(arg)
            if parsed is None:
                print("  (regex miss -> would go to LLM parser; skipped in demo)")
                continue
            # repeated text simulates a re-delivered message (same id)
            mid = 1 if arg == SCRIPT[0][1] else msg_id
            await executor.handle_parsed(parsed, channel_id=CHAN, topic_id=None,
                                         message_id=mid)
            if btc_link is None:
                btc_link = (await db.open_trades())[-1]["order_link_id"]
        elif action == "fill":
            link = btc_link if arg == "entry" else f"{btc_link}-{arg}"
            print(f"\n>>> exchange: fill {arg}")
            mock.simulate_fill(link)
        elif action == "price":
            sym, px = arg
            print(f"\n>>> market: {sym} -> {px}")
            mock.prices[sym] = Decimal(px)
        elif action == "panic":
            print("\n>>> operator: /panic")
            rg_mod.guard.panic()
        await mock.deliver(executor)

    print("\n=== final state ===")
    print(f"equity: {mock.equity} USDT")
    for t in await db.open_trades():
        print(f"open: {t['symbol']} {t['side']} qty={t['qty']} status={t['status']}")
    for sym, p in mock.positions.items():
        print(f"position: {sym} {p.side} size={p.size} sl={p.stop_loss}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
