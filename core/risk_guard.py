"""Risk guard: concurrency cap, daily-loss halt, pause flag, panic kill-switch."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone

from config import settings
from storage.db import db

log = logging.getLogger("risk")


def _local_midnight_iso() -> str:
    now = datetime.now()
    midnight = datetime.combine(now.date(), time.min)
    return midnight.astimezone(timezone.utc).isoformat()


@dataclass
class RiskState:
    paused: bool = False
    panicked: bool = False
    halted_daily_loss: bool = False
    reasons: list[str] = field(default_factory=list)


class RiskGuard:
    def __init__(self) -> None:
        self.state = RiskState()

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self.state.panicked = False
        self.state.halted_daily_loss = False

    def panic(self) -> None:
        self.state.panicked = True

    async def can_open_new(self, equity: float) -> tuple[bool, str]:
        if self.state.panicked:
            return False, "panic active (use /resume)"
        if self.state.paused:
            return False, "paused"
        if await self._daily_loss_breached(equity):
            return False, "daily loss limit reached"
        active = await db.count_active_positions()
        if active >= settings.max_concurrent_positions:
            return False, f"max concurrent positions ({settings.max_concurrent_positions})"
        return True, "ok"

    async def _daily_loss_breached(self, equity: float) -> bool:
        if equity <= 0:
            return False
        loss = await db.realized_pnl_since(_local_midnight_iso())
        if loss <= -abs(equity * settings.max_daily_loss_pct):
            if not self.state.halted_daily_loss:
                self.state.halted_daily_loss = True
                log.warning("daily loss halt triggered: %.4f", loss)
            return True
        return False


guard = RiskGuard()
