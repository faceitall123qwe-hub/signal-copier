"""Control bot (Bot API). Commands restricted to ALLOWED_USER_ID."""
from __future__ import annotations

import logging
from functools import wraps

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings
from core import notifier
from core.executor import Executor
from core.risk_guard import guard
from exchange.bybit_client import BybitClient
from storage.db import db

log = logging.getLogger("control")


def _restricted(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != settings.allowed_user_id:
            log.warning("unauthorized command from %s", uid)
            return
        return await func(self, update, context)

    return wrapper


class ControlBot:
    def __init__(self, executor: Executor, bybit: BybitClient, listener=None) -> None:
        self.executor = executor
        self.bybit = bybit
        self.listener = listener
        self.app: Application = (
            Application.builder().token(settings.control_bot_token).build()
        )
        self._register()

    def _register(self) -> None:
        h = self.app.add_handler
        h(CommandHandler("start", self.cmd_status))
        h(CommandHandler("status", self.cmd_status))
        h(CommandHandler("positions", self.cmd_positions))
        h(CommandHandler("pnl", self.cmd_pnl))
        h(CommandHandler("pause", self.cmd_pause))
        h(CommandHandler("resume", self.cmd_resume))
        h(CommandHandler("panic", self.cmd_panic))
        h(CommandHandler("risk", self.cmd_risk))
        h(CommandHandler("mode", self.cmd_mode))
        h(CommandHandler("sources", self.cmd_sources))
        h(CommandHandler("confirm", self.cmd_confirm))
        h(CommandHandler("reject", self.cmd_reject))

    # --- notifier binding ---
    async def _send(self, text: str) -> None:
        await self.app.bot.send_message(chat_id=settings.allowed_user_id, text=text)

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        notifier.bind(self._send)
        await notifier.notify(
            f"🤖 control bot online. MODE={settings.mode.value} "
            f"testnet={self.bybit.testnet} dry_run={self.bybit.dry_run}"
        )

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    # ================= commands =================
    @_restricted
    async def cmd_status(self, update, context):
        s = guard.state
        equity = await self.bybit.get_equity()
        active = await db.count_active_positions()
        await update.message.reply_text(
            f"MODE={settings.mode.value} testnet={self.bybit.testnet} "
            f"dry_run={self.bybit.dry_run}\n"
            f"paused={s.paused} panic={s.panicked} daily_halt={s.halted_daily_loss}\n"
            f"equity={equity} active_positions={active}/"
            f"{settings.max_concurrent_positions}\n"
            f"risk_pct={settings.risk_pct} max_lev={settings.max_leverage}"
        )

    @_restricted
    async def cmd_positions(self, update, context):
        trades = await db.open_trades()
        if not trades:
            await update.message.reply_text("no open trades")
            return
        lines = []
        for t in trades:
            scope = f"chan {t['source_channel_id']}"
            if t["topic_id"]:
                scope += f"/topic {t['topic_id']}"
            lines.append(
                f"#{t['id']} [{scope}] {t['symbol']} {t['side']} {t['status']} "
                f"qty={t['qty']} entry={t['entry_price']} sl={t['sl']}"
            )
        await update.message.reply_text("\n".join(lines))

    @_restricted
    async def cmd_pnl(self, update, context):
        from datetime import datetime, time, timezone
        midnight = datetime.combine(datetime.now().date(), time.min).astimezone(
            timezone.utc
        ).isoformat()
        pnl = await db.realized_pnl_since(midnight)
        equity = await self.bybit.get_equity()
        await update.message.reply_text(
            f"realized PnL today: {pnl:.4f}\n"
            f"daily loss limit: {-float(equity) * settings.max_daily_loss_pct:.4f}"
        )

    @_restricted
    async def cmd_pause(self, update, context):
        guard.pause()
        await update.message.reply_text("⏸️ paused — no new entries")

    @_restricted
    async def cmd_resume(self, update, context):
        guard.resume()
        await update.message.reply_text("▶️ resumed")

    @_restricted
    async def cmd_panic(self, update, context):
        guard.panic()
        try:
            await self.bybit.cancel_all()
            for t in await db.open_trades():
                if t["status"] in ("OPEN", "PARTIALLY_CLOSED"):
                    pos = await self.bybit.get_position(t["symbol"])
                    from decimal import Decimal
                    size = Decimal(str(pos["size"])) if pos and pos.get("size") else Decimal(str(t["qty"]))
                    await self.bybit.close_all(t["symbol"], t["side"] == "long", size)
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"panic errors: {e}")
        await update.message.reply_text(
            "🛑 PANIC: cancelled + flattened. New entries refused until /resume"
        )

    @_restricted
    async def cmd_risk(self, update, context):
        if not context.args:
            await update.message.reply_text(f"risk_pct={settings.risk_pct}")
            return
        try:
            val = float(context.args[0])
            settings.risk_pct = val
            await update.message.reply_text(f"risk_pct set to {val}")
        except ValueError:
            await update.message.reply_text("usage: /risk 0.01")

    @_restricted
    async def cmd_mode(self, update, context):
        await update.message.reply_text(
            f"MODE={settings.mode.value} (change via .env + restart; "
            f"LIVE needs CONFIRM_LIVE=I_UNDERSTAND)"
        )

    @_restricted
    async def cmd_sources(self, update, context):
        if not self.listener:
            await update.message.reply_text("listener not attached")
            return
        lines = ["connected sources:"]
        for cid in self.listener.channel_ids:
            forum = cid in self.listener.forum_channels
            lines.append(f"• {cid} {'(forum)' if forum else ''}")
            if forum:
                for (c, tid), title in self.listener.topic_titles.items():
                    if c == cid:
                        allowed = self.listener._allowed_topic(cid, tid)
                        lines.append(f"    topic {tid}: {title} "
                                     f"{'' if allowed else '[filtered]'}")
        await update.message.reply_text("\n".join(lines))

    @_restricted
    async def cmd_confirm(self, update, context):
        if not context.args:
            await update.message.reply_text("usage: /confirm <id>")
            return
        sid = int(context.args[0])
        ok = await self.executor.confirm(sid)
        await update.message.reply_text("confirmed" if ok else "no pending signal")

    @_restricted
    async def cmd_reject(self, update, context):
        if not context.args:
            await update.message.reply_text("usage: /reject <id>")
            return
        sid = int(context.args[0])
        ok = self.executor.reject(sid)
        await update.message.reply_text("rejected" if ok else "no pending signal")
