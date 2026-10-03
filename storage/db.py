"""SQLite persistence (aiosqlite). Trades, processed messages, pnl log."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

DB_PATH = "copier.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_channel_id INTEGER NOT NULL,
    topic_id INTEGER,
    source_msg_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_price REAL,
    sl REAL,
    tps_json TEXT,
    qty REAL,
    leverage INTEGER,
    order_link_id TEXT UNIQUE,
    exchange_order_ids_json TEXT,
    opened_at TEXT,
    closed_at TEXT,
    realized_pnl REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS processed_messages (
    source_channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    handled_at TEXT,
    PRIMARY KEY (source_channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS pnl_log (
    ts TEXT,
    source_channel_id INTEGER,
    symbol TEXT,
    realized_pnl REAL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_scope ON trades(source_channel_id, topic_id, status);
CREATE INDEX IF NOT EXISTS idx_trades_link ON trades(order_link_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ---- dedup ----
    async def is_processed(self, channel_id: int, message_id: int) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE source_channel_id=? AND message_id=?",
            (channel_id, message_id),
        )
        return await cur.fetchone() is not None

    async def mark_processed(self, channel_id: int, message_id: int) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO processed_messages VALUES (?,?,?)",
            (channel_id, message_id, _now()),
        )
        await self._conn.commit()

    # ---- trades ----
    async def trade_exists_for_link(self, order_link_id: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM trades WHERE order_link_id=?", (order_link_id,)
        )
        return await cur.fetchone() is not None

    async def create_trade(self, **kw: Any) -> int:
        kw.setdefault("status", "PENDING_ENTRY")
        kw.setdefault("opened_at", _now())
        kw["tps_json"] = json.dumps(kw.pop("tps", []))
        kw["exchange_order_ids_json"] = json.dumps(kw.pop("exchange_order_ids", []))
        cols = ",".join(kw.keys())
        ph = ",".join("?" for _ in kw)
        cur = await self._conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({ph})", tuple(kw.values())
        )
        await self._conn.commit()
        return cur.lastrowid

    async def update_trade(self, trade_id: int, **kw: Any) -> None:
        if "tps" in kw:
            kw["tps_json"] = json.dumps(kw.pop("tps"))
        if "exchange_order_ids" in kw:
            kw["exchange_order_ids_json"] = json.dumps(kw.pop("exchange_order_ids"))
        sets = ",".join(f"{k}=?" for k in kw)
        await self._conn.execute(
            f"UPDATE trades SET {sets} WHERE id=?", (*kw.values(), trade_id)
        )
        await self._conn.commit()

    async def get_trade(self, trade_id: int) -> Optional[dict]:
        cur = await self._conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_trade_by_link(self, order_link_id: str) -> Optional[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM trades WHERE order_link_id=?", (order_link_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def open_trades(self) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM trades WHERE status IN "
            "('PENDING_ENTRY','OPEN','PARTIALLY_CLOSED') ORDER BY id DESC"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def latest_open_for_scope(
        self, channel_id: int, topic_id: Optional[int]
    ) -> Optional[dict]:
        if topic_id is None:
            cur = await self._conn.execute(
                "SELECT * FROM trades WHERE source_channel_id=? AND topic_id IS NULL "
                "AND status IN ('OPEN','PARTIALLY_CLOSED','PENDING_ENTRY') "
                "ORDER BY id DESC LIMIT 1",
                (channel_id,),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM trades WHERE source_channel_id=? AND topic_id=? "
                "AND status IN ('OPEN','PARTIALLY_CLOSED','PENDING_ENTRY') "
                "ORDER BY id DESC LIMIT 1",
                (channel_id, topic_id),
            )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def open_trades_for_symbol(self, symbol: str) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM trades WHERE symbol=? AND status IN "
            "('OPEN','PARTIALLY_CLOSED','PENDING_ENTRY') ORDER BY id DESC",
            (symbol,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def count_active_positions(self) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE status IN ('OPEN','PARTIALLY_CLOSED')"
        )
        row = await cur.fetchone()
        return int(row["c"])

    # ---- pnl ----
    async def log_pnl(
        self, channel_id: int, symbol: str, pnl: float, reason: str
    ) -> None:
        await self._conn.execute(
            "INSERT INTO pnl_log VALUES (?,?,?,?,?)",
            (_now(), channel_id, symbol, pnl, reason),
        )
        await self._conn.commit()

    async def realized_pnl_since(self, iso_ts: str) -> float:
        cur = await self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) s FROM pnl_log WHERE ts>=?",
            (iso_ts,),
        )
        row = await cur.fetchone()
        return float(row["s"])


db = DB()
