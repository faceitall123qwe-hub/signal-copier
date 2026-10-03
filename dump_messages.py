"""Dump recent messages from a source channel to inspect signal formats.

Runs locally on YOUR machine using the existing Telethon session (secondary
account that is a member of the channel). Nothing is sent anywhere — output
goes to stdout and logs/dump.txt so you can paste it into chat.

Usage:
    python dump_messages.py                 # first channel in SOURCE_CHANNELS
    python dump_messages.py @somechannel    # explicit channel
    python dump_messages.py -1001234567 80  # explicit channel + how many msgs
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

from telethon import TelegramClient, utils

from config import settings


def _topic_id(message) -> Optional[int]:
    r = getattr(message, "reply_to", None)
    if r is None:
        return None
    if getattr(r, "forum_topic", False):
        return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)
    return None


async def main() -> None:
    args = sys.argv[1:]
    ref = args[0] if args else (settings.source_channel_list or [None])[0]
    if ref is None:
        print("No channel given and SOURCE_CHANNELS is empty.")
        return
    limit = int(args[1]) if len(args) > 1 else 50

    client = TelegramClient(
        settings.telegram_session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start()

    target = int(ref) if str(ref).lstrip("-").isdigit() else ref
    ent = await client.get_entity(target)
    cid = utils.get_peer_id(ent)
    is_forum = getattr(ent, "forum", False)

    lines: list[str] = []
    header = (
        f"=== {getattr(ent, 'title', ref)} | id={cid} | "
        f"forum={is_forum} | last {limit} text messages ==="
    )
    lines.append(header)

    async for msg in client.iter_messages(ent, limit=limit):
        text = (msg.message or "").strip()
        if not text:
            continue
        tid = _topic_id(msg) if is_forum else None
        scope = f"[topic {tid}] " if tid else ""
        lines.append("-" * 60)
        lines.append(f"{scope}msg_id={msg.id} date={msg.date}")
        lines.append(text)

    out = "\n".join(lines)
    print(out)
    with open("logs/dump.txt", "w", encoding="utf-8") as f:
        f.write(out)
    print(f"\n[saved to logs/dump.txt — {len(lines)} lines]")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
