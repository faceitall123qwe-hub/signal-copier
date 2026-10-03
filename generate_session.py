"""One-time interactive Telethon login for the userbot (secondary account).

Run on the SECONDARY account that is a member of every source channel:
    python generate_session.py

Produces a <TELEGRAM_SESSION>.session file (gitignored). Never commit it.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from config import settings


async def main() -> None:
    client = TelegramClient(
        settings.telegram_session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start()  # prompts for phone + code (+ 2FA) on first run
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (id={me.id}, @{me.username})")
    print(f"Session saved to: {settings.telegram_session}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
