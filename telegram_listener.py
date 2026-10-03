"""Telethon userbot: reads all configured sources (channels + forum topics),
dedups, parses, and feeds the executor. Never blocks the event loop."""
from __future__ import annotations

import logging
from typing import Optional

from telethon import TelegramClient, events, utils
from telethon.tl.functions.channels import GetForumTopicsRequest

from config import settings
from core.executor import Executor
from parser import parse_message
from storage.db import db

log = logging.getLogger("listener")


class Listener:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.client = TelegramClient(
            settings.telegram_session,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        self.entities: list = []
        self.channel_ids: list[int] = []
        self.forum_channels: set[int] = set()
        self.topic_titles: dict[tuple[int, int], str] = {}

    async def start(self) -> None:
        await self.client.start()
        await self._resolve_sources()
        self._register_handlers()
        log.info("listener started; sources=%s", self.channel_ids)

    async def _resolve_sources(self) -> None:
        for ref in settings.source_channel_list:
            try:
                ent = await self.client.get_entity(
                    int(ref) if ref.lstrip("-").isdigit() else ref
                )
            except Exception as e:  # noqa: BLE001
                log.error("cannot resolve source %s: %s", ref, e)
                continue
            self.entities.append(ent)
            cid = utils.get_peer_id(ent)
            self.channel_ids.append(cid)
            if getattr(ent, "forum", False):
                self.forum_channels.add(cid)
                await self._load_topics(ent, cid)

    async def _load_topics(self, ent, cid: int) -> None:
        try:
            res = await self.client(
                GetForumTopicsRequest(
                    channel=ent, offset_date=None, offset_id=0,
                    offset_topic=0, limit=100,
                )
            )
            for t in res.topics:
                title = getattr(t, "title", "")
                self.topic_titles[(cid, t.id)] = title
            log.info("forum %s topics: %s", cid,
                     [(tid, ti) for (c, tid), ti in self.topic_titles.items() if c == cid])
        except Exception as e:  # noqa: BLE001
            log.warning("could not enumerate topics for %s: %s", cid, e)

    def _topic_id(self, message) -> Optional[int]:
        r = getattr(message, "reply_to", None)
        if r is None:
            return None
        if getattr(r, "forum_topic", False):
            return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)
        return None

    def _allowed_topic(self, cid: int, topic_id: Optional[int]) -> bool:
        allow = settings.topic_allowlist
        if cid not in allow:
            return True  # no restriction for this channel
        if topic_id is None:
            return False
        return topic_id in allow[cid]

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage(chats=self.entities))
        async def _on_new(event):  # noqa: ANN001
            await self._handle_event(event)

        if settings.react_to_edits:
            @self.client.on(events.MessageEdited(chats=self.entities))
            async def _on_edit(event):  # noqa: ANN001
                await self._handle_event(event, edited=True)

    async def _handle_event(self, event, edited: bool = False) -> None:
        try:
            msg = event.message
            text = msg.message or ""
            if not text.strip():
                return
            cid = utils.get_peer_id(await event.get_chat())
            topic_id = self._topic_id(msg) if cid in self.forum_channels else None

            if not self._allowed_topic(cid, topic_id):
                return

            # dedup on (channel, message_id). Edits reuse same id -> reprocess only
            # if REACT_TO_EDITS (use composite key so edit isn't blocked).
            dedup_id = msg.id if not edited else -msg.id
            if await db.is_processed(cid, dedup_id):
                return
            await db.mark_processed(cid, dedup_id)

            log.info("msg chan=%s topic=%s id=%s edited=%s: %s",
                     cid, topic_id, msg.id, edited, text[:80].replace("\n", " "))

            parsed = await parse_message(text)
            await self.executor.handle_parsed(
                parsed, channel_id=cid, topic_id=topic_id, message_id=msg.id
            )
        except Exception as e:  # noqa: BLE001
            log.exception("listener handle error: %s", e)

    async def run_forever(self) -> None:
        await self.client.run_until_disconnected()
