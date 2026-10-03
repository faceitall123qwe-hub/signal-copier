"""Parser facade: regex fast-path → LLM fallback."""
from __future__ import annotations

import logging

from . import llm_parser, regex_fastpath
from .schema import Intent, ParsedSignal, Side

log = logging.getLogger("parser")


async def parse_message(text: str) -> ParsedSignal:
    fast = regex_fastpath.parse(text)
    if fast is not None and fast.confidence >= 0.8:
        log.debug("regex fast-path hit: %s conf=%.2f", fast.intent, fast.confidence)
        return fast
    return await llm_parser.parse(text)


__all__ = ["parse_message", "ParsedSignal", "Intent", "Side"]
