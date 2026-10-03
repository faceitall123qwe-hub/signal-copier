"""Cheap regex parsing for clean, unambiguous signals.

Returns a ParsedSignal with high confidence when the message clearly matches a
known shape, else None so the LLM fallback runs. Handles EN/RU/PL keywords.
"""
from __future__ import annotations

import re
from typing import Optional

from .schema import Intent, ParsedSignal, Side

# --- keyword tables (lowercased) ---
LONG_WORDS = ("long", "buy", "лонг", "купить", "покупка", "kupno", "longa")
SHORT_WORDS = ("short", "sell", "шорт", "продать", "продажа", "sprzedaz", "krotka")

BE_WORDS = ("breakeven", "break even", "b/e", "be", "безубыток", "в бу", "вбу", "na bezpieczne")
FULL_CLOSE_WORDS = ("close all", "full close", "close position", "закрыть", "закрой",
                    "закрытие", "exit all", "zamknij", "zamkniecie", "close 100")
PARTIAL_WORDS = ("partial", "take partial", "частично", "частичное", "close half",
                 "частичный", "zamknij czesc")
TP_WORDS = ("tp", "take profit", "target", "тейк", "тп", "цель", "tp1", "tp2", "tp3")
SL_WORDS = ("sl", "stop", "stoploss", "stop loss", "стоп", "сл")

NUM = r"[-+]?\d+(?:[.,]\d+)?"


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def _nums(text: str) -> list[float]:
    return [_f(m) for m in re.findall(NUM, text)]


STOP_TOKENS = {
    "LONG", "SHORT", "BUY", "SELL", "TP", "SL", "USDT", "USD", "ENTRY", "STOP",
    "TARGET", "SIGNAL", "TRADER", "ID", "PERP", "RISK",
    "CLOSE", "ALL", "FULL", "FULLY", "POSITION", "NOW", "EXIT", "PARTIAL",
    "HALF", "TAKE", "PROFIT", "MOVE", "TO", "BE", "BREAKEVEN", "MARKET", "LIMIT",
}


def _find_symbol(text: str) -> Optional[str]:
    up = text.upper()
    # 1) explicit USDT pair (most reliable): BTCUSDT, XAUTUSDT, ARUSDT
    m = re.search(r"\b([A-Z]{2,15}USDT)\b", up)
    if m:
        return m.group(1)
    # 2) #/$ tagged ticker: #ETH, $BTC
    m = re.search(r"[#$]([A-Z]{2,15})\b", up)
    if m and m.group(1) not in STOP_TOKENS:
        return m.group(1)
    # 3) bare uppercase ticker, skipping keywords (Cyrillic words are ignored)
    for c in re.findall(r"\b([A-Z]{2,15})\b", up):
        if c not in STOP_TOKENS:
            return c
    return None


def _side(text: str) -> Optional[Side]:
    t = text.lower()
    has_long = any(w in t for w in LONG_WORDS)
    has_short = any(w in t for w in SHORT_WORDS)
    if has_long and not has_short:
        return Side.LONG
    if has_short and not has_long:
        return Side.SHORT
    return None


def _pct(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if m:
        return _f(m.group(1))
    return None


def parse(text: str) -> Optional[ParsedSignal]:
    if not text or not text.strip():
        return ParsedSignal(intent=Intent.NOISE, confidence=0.99, raw=text)

    t = text.lower()

    # --- breakeven move (management) ---
    if any(w in t for w in BE_WORDS) and any(w in t for w in SL_WORDS + ("sl",)):
        return ParsedSignal(
            intent=Intent.MODIFY_SL, symbol=_find_symbol(text),
            sl_to_breakeven=True, confidence=0.9, raw=text,
        )
    if any(p in t for p in ("sl to be", "stop to be", "move sl", "стоп в бу", "сл в бу")):
        return ParsedSignal(
            intent=Intent.MODIFY_SL, symbol=_find_symbol(text),
            sl_to_breakeven=True, confidence=0.9, raw=text,
        )

    # --- full close ---
    full_close = any(w in t for w in FULL_CLOSE_WORDS) or (
        ("close" in t or "закр" in t or "zamkn" in t)
        and ("position" in t or "all" in t or "fully" in t
             or "полностью" in t or "позиц" in t)
    )
    if full_close and not any(w in t for w in PARTIAL_WORDS) and "%" not in t:
        return ParsedSignal(
            intent=Intent.FULL_CLOSE, symbol=_find_symbol(text),
            close_pct=100.0, confidence=0.88, raw=text,
        )

    # --- partial close ---
    if any(w in t for w in PARTIAL_WORDS):
        pct = _pct(text) or 50.0
        return ParsedSignal(
            intent=Intent.PARTIAL_CLOSE, symbol=_find_symbol(text),
            close_pct=pct, confidence=0.85, raw=text,
        )

    # --- modify SL to explicit price ---
    sl_match = re.search(r"(?:sl|stop(?:\s*loss)?|стоп|сл)\D{0,6}(" + NUM + r")", t)

    # --- new entry: needs side + at least one entry number + SL ---
    side = _side(text)
    if side is not None:
        # collect entry / sl / tps by labelled sections
        entries: list[float] = []
        ent_match = re.search(
            r"(?:entry|enter|вход|вхід|wejscie|@|цена)\D{0,4}(" + NUM + r"(?:\s*[-–]\s*" + NUM + r")?)",
            t,
        )
        if ent_match:
            entries = _nums(ent_match.group(1))
        sl_val = _f(sl_match.group(1)) if sl_match else None
        tps: list[float] = []
        for m in re.finditer(r"(?:tp\d?|target|тейк|тп|цель|take\s*profit)\D{0,4}(" + NUM + r")", t):
            tps.append(_f(m.group(1)))

        # "Вход: рынок" / "по рынку" / "по текущим" => market entry, no price
        market_entry = bool(re.search(r"рынок|market|по\s*текущ|по\s*рынк", t))

        if sl_val is not None and (entries or market_entry):
            return ParsedSignal(
                intent=Intent.NEW_ENTRY, symbol=_find_symbol(text), side=side,
                entries=entries, stop_loss=sl_val, take_profits=tps,
                confidence=0.9 if tps else 0.82, raw=text,
            )
        # side present but structure unclear (e.g. management msg) -> LLM decides
        return None

    # --- SL modify without side context ---
    if sl_match and not side and any(w in t for w in SL_WORDS):
        # ambiguous between new entry and modify; defer to LLM unless very short
        return None

    return None
