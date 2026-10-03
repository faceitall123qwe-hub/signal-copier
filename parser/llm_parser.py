"""LLM fallback parser using Anthropic (Claude Haiku by default).

Called only when the regex fast-path misses. Returns a validated ParsedSignal;
on repeated failure returns confidence 0 so the caller gates it.
"""
from __future__ import annotations

import json
import logging

from anthropic import AsyncAnthropic

from config import settings
from .examples import EXAMPLES
from .schema import Intent, ParsedSignal

log = logging.getLogger("parser.llm")

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

SYSTEM = """You are a trade-signal parser for crypto USDT perpetual futures.
You receive ONE Telegram message (language may be English, Russian or Polish).
Classify its intent and extract fields. Return ONLY a single JSON object, no prose,
no markdown fences.

JSON schema (use these exact keys):
{
  "intent": "NEW_ENTRY|MODIFY_SL|ADD_REPLACE_TP|PARTIAL_CLOSE|FULL_CLOSE|INFO|NOISE",
  "symbol": "string base+USDT like BTCUSDT, or null",
  "side": "long|short|null",
  "entries": [numbers],
  "stop_loss": number|null,
  "take_profits": [numbers],
  "leverage": integer|null,
  "close_pct": number 0-100 or null,
  "sl_to_breakeven": true|false,
  "confidence": number 0..1
}

Rules:
- NEW_ENTRY only when a direction AND at least one entry price are present.
- "move stop to entry/breakeven/BE" => intent MODIFY_SL, sl_to_breakeven true.
- "stop to <price>" => MODIFY_SL with stop_loss set.
- "new targets / replace TP" => ADD_REPLACE_TP with take_profits.
- "close X%" => PARTIAL_CLOSE with close_pct; "close all/fully" => FULL_CLOSE close_pct 100.
- Commentary/news/greetings => NOISE. Market analysis without an actionable order => INFO.
- confidence reflects how certain you are it is actionable and unambiguous.
"""


def _build_messages(text: str) -> list[dict]:
    msgs: list[dict] = []
    for raw, expected in EXAMPLES:
        msgs.append({"role": "user", "content": raw})
        msgs.append({"role": "assistant", "content": json.dumps(expected)})
    msgs.append({"role": "user", "content": text})
    return msgs


def _coerce(text: str, payload: dict) -> ParsedSignal:
    payload = dict(payload)
    payload["raw"] = text
    return ParsedSignal.model_validate(payload)


async def parse(text: str) -> ParsedSignal:
    messages = _build_messages(text)
    for attempt in range(2):
        try:
            resp = await _client.messages.create(
                model=settings.parser_model,
                max_tokens=512,
                temperature=0,
                system=SYSTEM,
                messages=messages,
            )
            content = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            content = _strip_fences(content)
            payload = json.loads(content)
            return _coerce(text, payload)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM parse attempt %d failed: %s", attempt + 1, e)
            messages = _build_messages(text)  # reset; retry once
    log.error("LLM parse failed permanently for message")
    return ParsedSignal(intent=Intent.NOISE, confidence=0.0, raw=text)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
        if s.startswith("json"):
            s = s[4:].strip()
    # take the outermost JSON object if extra text slipped in
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1:
        s = s[start : end + 1]
    return s
