from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    DRY_RUN = "DRY_RUN"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram (Telethon userbot)
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str = "copier"
    source_channels: str = ""
    source_topic_ids: str = ""
    react_to_edits: bool = True

    # Control bot
    control_bot_token: str
    allowed_user_id: int

    # Bybit
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool = True

    # Parser
    anthropic_api_key: str
    parser_model: str = "claude-haiku-4-5-20251001"
    min_confidence: float = 0.75

    # Risk / sizing
    risk_pct: float = 0.01
    default_leverage: int = 10
    max_leverage: int = 10
    max_concurrent_positions: int = 3
    max_daily_loss_pct: float = 0.05
    margin_mode: str = "isolated"
    fee_buffer: float = 0.005

    # Execution
    entry_order_type: str = "limit"
    max_slippage_pct: float = 0.3
    entry_fill_timeout_min: int = 30
    tp_split: str = "equal"
    move_sl_to_be_after_tp1: bool = True
    on_insufficient_margin: str = "skip"   # skip | reduce
    on_symbol_collision: str = "skip"      # skip | allow_same_side | subaccount
    fallback_stop_pct: Optional[float] = None

    # Mode gating
    mode: Mode = Mode.DRY_RUN
    confirm_live: str = ""

    @field_validator("fallback_stop_pct", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v in ("", None):
            return None
        return v

    # ----- derived helpers -----
    @property
    def source_channel_list(self) -> list[str]:
        return [c.strip() for c in self.source_channels.split(",") if c.strip()]

    @property
    def topic_allowlist(self) -> dict[int, set[int]]:
        """Parse SOURCE_TOPIC_IDS -> {channel_id: {topic_id,...}}.

        Format: -1001234567:5,12  (multiple groups separated by spaces or ';')
        """
        out: dict[int, set[int]] = {}
        raw = self.source_topic_ids.strip()
        if not raw:
            return out
        for group in raw.replace(";", " ").split():
            if ":" not in group:
                continue
            chan, topics = group.split(":", 1)
            try:
                cid = int(chan)
            except ValueError:
                continue
            tset = {int(t) for t in topics.split(",") if t.strip()}
            if tset:
                out[cid] = tset
        return out

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE

    @property
    def places_orders(self) -> bool:
        return self.mode in (Mode.TESTNET, Mode.LIVE)

    @property
    def use_testnet_endpoint(self) -> bool:
        # TESTNET mode always uses testnet. LIVE uses bybit_testnet flag (should be false).
        if self.mode == Mode.TESTNET:
            return True
        return self.bybit_testnet

    def validate_mode_gate(self) -> None:
        if self.mode == Mode.LIVE and self.confirm_live != "I_UNDERSTAND":
            raise SystemExit(
                "Refusing to start LIVE: set MODE=LIVE and CONFIRM_LIVE=I_UNDERSTAND"
            )


settings = Settings()
