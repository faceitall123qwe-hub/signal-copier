from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Intent(str, Enum):
    NEW_ENTRY = "NEW_ENTRY"
    MODIFY_SL = "MODIFY_SL"
    ADD_REPLACE_TP = "ADD_REPLACE_TP"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"
    INFO = "INFO"
    NOISE = "NOISE"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class ParsedSignal(BaseModel):
    intent: Intent
    symbol: Optional[str] = None
    side: Optional[Side] = None
    entries: list[float] = Field(default_factory=list)
    stop_loss: Optional[float] = None
    take_profits: list[float] = Field(default_factory=list)
    leverage: Optional[int] = None
    close_pct: Optional[float] = None
    sl_to_breakeven: bool = False
    confidence: float = 0.0
    raw: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def _norm_symbol(cls, v):
        if not v:
            return None
        s = str(v).upper().replace("/", "").replace("-", "").replace(" ", "")
        if s.endswith("PERP"):
            s = s[:-4]
        # Normalise common quote spellings to USDT linear contract
        if s.endswith("USD") and not s.endswith("USDT"):
            s = s + "T"
        if not s.endswith("USDT"):
            s = s + "USDT"
        return s

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v):
        return max(0.0, min(1.0, float(v)))

    @field_validator("close_pct")
    @classmethod
    def _clamp_pct(cls, v):
        if v is None:
            return v
        v = float(v)
        if v <= 1.0:  # allow fraction form 0..1
            v = v * 100.0
        return max(0.0, min(100.0, v))
