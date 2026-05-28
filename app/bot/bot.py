from __future__ import annotations

import logging

import aiosqlite
from telegram.ext import Application, CommandHandler
from telethon import TelegramClient

from app.bot import handlers
from app.config import settings

log = logging.getLogger(__name__)


def create_bot_app(
    tg_client: TelegramClient,
    db_conn: aiosqlite.Connection,
) -> Application:
    app = Application.builder().token(settings.control_bot_token).build()

    app.bot_data["tg_client"] = tg_client
    app.bot_data["db_conn"] = db_conn

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("status", handlers.cmd_status))
    app.add_handler(CommandHandler("stats", handlers.cmd_stats))
    app.add_handler(CommandHandler("stats_by_chat", handlers.cmd_stats_by_chat))
    app.add_handler(CommandHandler("dialogs", handlers.cmd_dialogs))
    app.add_handler(CommandHandler("search", handlers.cmd_search))
    app.add_handler(CommandHandler("backfill", handlers.cmd_backfill))
    app.add_handler(CommandHandler("confirm_backfill", handlers.cmd_confirm_backfill))
    app.add_handler(CommandHandler("exportusershistory", handlers.cmd_exportusershistory))

    return app
