from __future__ import annotations

import logging

import aiosqlite
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telethon import TelegramClient

from app.bot import handlers
from app.config import settings

log = logging.getLogger(__name__)


async def setup_commands(app: Application) -> None:
    """Publish the command list to Telegram's native "Menu" button.

    Must be called explicitly: we drive the Application lifecycle manually,
    and PTB only runs post_init inside run_polling/run_webhook.
    """
    try:
        await app.bot.set_my_commands(handlers.bot_commands())
    except Exception:
        log.warning("Failed to set bot command menu", exc_info=True)


def create_bot_app(
    tg_client: TelegramClient,
    db_conn: aiosqlite.Connection,
) -> Application:
    app = Application.builder().token(settings.control_bot_token).build()

    app.bot_data["tg_client"] = tg_client
    app.bot_data["db_conn"] = db_conn

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("status", handlers.cmd_status))
    app.add_handler(CommandHandler("stats", handlers.cmd_stats))
    app.add_handler(CommandHandler("stats_by_chat", handlers.cmd_stats_by_chat))
    app.add_handler(CommandHandler("dialogs", handlers.cmd_dialogs))
    app.add_handler(CommandHandler("search", handlers.cmd_search))
    app.add_handler(CommandHandler("backfill", handlers.cmd_backfill))
    app.add_handler(CommandHandler("confirm_backfill", handlers.cmd_confirm_backfill))
    app.add_handler(CommandHandler("exportusershistory", handlers.cmd_exportusershistory))

    app.add_handler(CommandHandler("notify", handlers.cmd_notify))
    app.add_handler(CommandHandler("notify_on", handlers.cmd_notify_on))
    app.add_handler(CommandHandler("notify_off", handlers.cmd_notify_off))
    app.add_handler(CommandHandler("notify_del", handlers.cmd_notify_del))

    # /notify_add and /notify_edit drive the same sequential add/edit dialog
    text_input = filters.TEXT & ~filters.COMMAND
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("notify_add", handlers.cmd_notify_add),
                CommandHandler("notify_edit", handlers.cmd_notify_edit),
            ],
            states={
                handlers.NOTIFY_NAME: [MessageHandler(text_input, handlers.notify_name)],
                handlers.NOTIFY_KEYWORDS: [MessageHandler(text_input, handlers.notify_keywords)],
                handlers.NOTIFY_ARCHIVED: [
                    CallbackQueryHandler(handlers.notify_archived, pattern=r"^notify_arch:")
                ],
                handlers.NOTIFY_ACTIVE: [
                    CallbackQueryHandler(handlers.notify_active, pattern=r"^notify_active:")
                ],
            },
            fallbacks=[CommandHandler("cancel", handlers.notify_cancel)],
        )
    )

    # persistent-keyboard taps arrive as plain text matching a button label
    app.add_handler(MessageHandler(filters.Text(handlers.BUTTON_LABELS), handlers.on_button))

    return app
