"""Entrypoint: wires listener + parser + executor + control bot + Bybit WS."""
from __future__ import annotations

import asyncio
import logging
import sys

from config import settings
from control_bot import ControlBot
from core import notifier
from core.executor import Executor
from exchange.bybit_client import BybitClient
from storage.db import db
from telegram_listener import Listener

LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/copier.log", encoding="utf-8"),
        ],
    )
    # never let third-party libs spam secrets at DEBUG
    for noisy in ("httpx", "telethon", "pybit", "httpcore", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def recover_state(bybit: BybitClient, executor: Executor) -> None:
    """Reconcile persisted open trades against live positions after a restart.

    Idempotent orderLinkIds mean we never double-fire; here we just resync
    status so the state machine reflects reality.
    """
    if bybit.dry_run:
        return
    from core import state
    from decimal import Decimal

    for t in await db.open_trades():
        try:
            pos = await bybit.get_position(t["symbol"])
            size = Decimal(str(pos["size"])) if pos and pos.get("size") else Decimal(0)
            if size == 0 and t["status"] in (state.OPEN, state.PARTIALLY_CLOSED):
                await db.update_trade(t["id"], status=state.CLOSED)
            elif size != 0 and t["status"] == state.PENDING_ENTRY:
                await db.update_trade(t["id"], status=state.OPEN)
        except Exception:  # noqa: BLE001
            logging.getLogger("recover").exception("recover %s", t["symbol"])


async def main() -> None:
    setup_logging()
    log = logging.getLogger("main")
    settings.validate_mode_gate()

    log.info("starting in MODE=%s (testnet=%s dry_run=%s)",
             settings.mode.value, settings.use_testnet_endpoint,
             not settings.places_orders)

    await db.connect()

    bybit = BybitClient()
    executor = Executor(bybit)
    executor.attach_loop()

    listener = Listener(executor)
    control = ControlBot(executor, bybit, listener)

    await control.start()           # binds notifier
    await recover_state(bybit, executor)
    await listener.start()

    bybit.start_private_ws(
        on_order=executor.on_order,
        on_position=executor.on_position,
        on_execution=executor.on_execution,
    )

    await notifier.notify("✅ signal-copier ready")

    try:
        await listener.run_forever()
    finally:
        await control.stop()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        print(f"shutdown: {e}")
