from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys

from telethon import events
from telethon.tl.types import (
    Message,
    MessageActionPhoneCall,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaGeo,
    MessageMediaContact,
    MessageMediaWebPage,
)
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo, DocumentAttributeSticker
from telethon.tl.types import UpdateDialogFilter, UpdateDialogFilterOrder, UpdateDialogFilters

from app.bot.bot import create_bot_app, setup_commands
from app.config import settings
from app.db import get_connection, init_db
from app.folders import ChatWhitelist
from app.ingest import save_message
from app.services import notifications, retention
from app.telegram_client import SessionNotAuthorized, create_client, start_client

log = logging.getLogger(__name__)

# systemd: don't ask for a code, exit with this status (unit sets
# RestartPreventExitStatus) and wait for a manual `python -m app.login`.
EXIT_NOT_AUTHORIZED = 78
# folder refresh + storage limit check
MAINTENANCE_INTERVAL = 600

LOGIN_HINT = (
    "Сессия Telegram недействительна, сервер остановлен.\n"
    "Войдите заново по SSH: python -m app.login (от пользователя сервиса), "
    "затем перезапустите сервис."
)

# ANSI colors — only on an interactive terminal (not under systemd/journald)
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_WHITE = "\033[97m"
if not _USE_COLOR:
    C_RESET = C_DIM = C_BOLD = C_GREEN = C_YELLOW = C_RED = C_CYAN = C_MAGENTA = C_WHITE = ""

def _format_duration(seconds: int | float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _format_message_preview(msg: Message) -> str:
    if msg.action and isinstance(msg.action, MessageActionPhoneCall):
        duration = getattr(msg.action, "duration", None)
        verb = "outgoing" if msg.out else "incoming"
        if duration:
            return f"{verb} call {_format_duration(duration)}"
        return f"{verb} call (missed)"

    if msg.text and not msg.media:
        return msg.text.replace("\n", " ")[:80]

    if isinstance(msg.media, MessageMediaPhoto):
        caption = (msg.text or "").replace("\n", " ")[:40]
        return f"photo{': ' + caption if caption else ''}"

    if isinstance(msg.media, MessageMediaDocument) and msg.media.document:
        doc = msg.media.document
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeAudio):
                dur = _format_duration(attr.duration) if attr.duration else ""
                if attr.voice:
                    return f"voice {dur}"
                title = attr.title or attr.performer or ""
                return f"audio {dur} {title}".strip()
            if isinstance(attr, DocumentAttributeVideo):
                dur = _format_duration(attr.duration) if attr.duration else ""
                if attr.round_message:
                    return f"video msg {dur}"
                return f"video {dur}"
            if isinstance(attr, DocumentAttributeSticker):
                alt = attr.alt or ""
                return f"sticker {alt}"
        caption = (msg.text or "").replace("\n", " ")[:40]
        return f"file{': ' + caption if caption else ''}"

    if isinstance(msg.media, MessageMediaGeo):
        return "location"

    if isinstance(msg.media, MessageMediaContact):
        return f"contact: {msg.media.first_name or ''} {msg.media.last_name or ''}".strip()

    if isinstance(msg.media, MessageMediaWebPage):
        text = (msg.text or "").replace("\n", " ")[:80]
        return text or "link"

    if msg.text:
        return msg.text.replace("\n", " ")[:80]

    return f"({type(msg.media).__name__})" if msg.media else "(empty)"


BANNER = f"""
{C_CYAN}{C_BOLD}  ╔══════════════════════════════════════════╗
  ║       TELEGRAM INGESTION SERVER          ║
  ║              headless-tgclient           ║
  ╚══════════════════════════════════════════╝{C_RESET}
"""


class _ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: C_DIM,
        logging.INFO: C_GREEN,
        logging.WARNING: C_YELLOW,
        logging.ERROR: C_RED,
        logging.CRITICAL: f"{C_RED}{C_BOLD}",
    }
    LEVEL_ICONS = {
        logging.DEBUG: "   ",
        logging.INFO: " + ",
        logging.WARNING: " ! ",
        logging.ERROR: " x ",
        logging.CRITICAL: "!! ",
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, C_RESET)
        icon = self.LEVEL_ICONS.get(record.levelno, "   ")
        ts = self.formatTime(record, "%H:%M:%S")

        name = record.name
        if name.startswith("app."):
            name = name[4:]
        elif name == "__main__":
            name = "core"

        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"

        return f"  {C_DIM}{ts}{C_RESET}  {color}{icon}{C_RESET} {C_DIM}{name:<18}{C_RESET} {msg}"


def _setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColorFormatter())
    handlers: list[logging.Handler] = [console]

    log_path = settings.log_path
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 2**20, backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telethon.network").setLevel(logging.WARNING)
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)


def _print_status(label: str, value: str, ok: bool = True) -> None:
    check = f"{C_GREEN}OK{C_RESET}" if ok else f"{C_RED}FAIL{C_RESET}"
    print(f"  {C_DIM}{'':>8}{C_RESET}  {C_WHITE}{label:<22}{C_RESET} {value} [{check}]")


async def _alert_owner(text: str) -> None:
    """DM the owner via the control bot (works without the user client)."""
    try:
        from telegram import Bot

        async with Bot(settings.control_bot_token) as bot:
            await bot.send_message(chat_id=settings.allowed_telegram_user_id, text=text)
    except Exception:
        log.warning("Could not alert the owner via the control bot", exc_info=True)


async def _enforce_storage(conn) -> None:
    if settings.max_storage_mb > 0:
        await retention.enforce_storage_limit(conn, settings.max_storage_mb * 2**20, settings.tz)


async def _run() -> None:
    if sys.platform == "win32":
        os.system("")  # enable ANSI escape codes in the Windows console
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    _setup_logging()
    print(BANNER)

    await init_db()
    conn = await get_connection()
    _print_status("Database", str(settings.db_path.name), True)
    await _enforce_storage(conn)

    client = create_client()
    interactive = sys.stdin is not None and sys.stdin.isatty()
    try:
        me_id = await start_client(client, interactive=interactive)
    except SessionNotAuthorized:
        log.error("Telegram session is not authorized. Run: python -m app.login")
        await _alert_owner(LOGIN_HINT)
        await conn.close()
        raise SystemExit(EXIT_NOT_AUTHORIZED)
    me = await client.get_me()
    _print_status("Telegram client", f"{me.first_name} (id={me_id})", True)

    msg_count = await conn.execute_fetchall("SELECT COUNT(*) FROM messages")
    _print_status("Messages indexed", str(msg_count[0][0]), True)

    # chat whitelist by Telegram folder (CHAT_FOLDERS); fails closed
    whitelist = ChatWhitelist(settings.chat_folder_names)
    if whitelist.enabled:
        try:
            await whitelist.refresh(client)
        except Exception:
            log.exception("Could not load Telegram folders; no chats processed until next refresh")
    _print_status("Chats", whitelist.describe(), not whitelist.enabled or bool(whitelist.chat_ids))

    @client.on(events.Raw(types=(
        UpdateDialogFilter, UpdateDialogFilters, UpdateDialogFilterOrder,
    )))
    async def on_folders_changed(update):
        try:
            await whitelist.refresh(client)
        except Exception:
            log.exception("Folder refresh failed")

    # cache archived chat IDs at startup for filtering
    _archived_ids: set[int] = set()
    if settings.skip_archived:
        async for d in client.iter_dialogs(archived=True):
            _archived_ids.add(d.id)
        log.info("Cached %d archived chats for filtering", len(_archived_ids))

    # resolved once the control bot is up; closures read them at call time
    notify_target = None
    bot_id = None
    notify_bot = None
    # the bot DMs the owner (alert push)
    owner_id = settings.allowed_telegram_user_id

    @client.on(events.NewMessage)
    async def on_new_message(event):
        msg = event.message
        if not isinstance(msg, Message):
            return
        # outside the configured folder(s): don't store, don't notify
        if not whitelist.allows(msg.chat_id):
            return

        is_archived = msg.chat_id in _archived_ids

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)

        # keyword alerts run on every chat, regardless of archive/mute state,
        # and independently of whether the chat is ingested
        try:
            await notifications.process_message(
                client, conn, msg, chat_title, is_archived, notify_target, bot_id,
                bot=notify_bot, owner_id=owner_id,
            )
        except Exception:
            log.exception("Notification processing failed")

        # a chat explicitly in the folder is ingested even when archived
        if settings.skip_archived and is_archived and not whitelist.enabled:
            return

        inserted = await save_message(conn, msg, chat_title=chat_title)
        if inserted:
            await conn.commit()
            preview = _format_message_preview(msg)
            if msg.out:
                log.info("%s%s>>>%s %s: %s", C_CYAN, C_BOLD, C_RESET, chat_title or "?", preview)
            else:
                s = msg.sender
                if s is None:
                    try:
                        s = await msg.get_sender()
                    except Exception:
                        pass
                if s is None:
                    sender = "?"
                elif hasattr(s, "first_name"):
                    sender = (s.first_name or "") or (getattr(s, "last_name", "") or "") or "?"
                else:
                    sender = chat_title or "?"
                log.info("%s<<<  %s%s | %s: %s", C_MAGENTA, chat_title or "?", C_RESET, sender, preview)

    _print_status("Live ingestion", "listening", True)

    bot_app = create_bot_app(client, conn, whitelist)

    async def _maintenance() -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL)
            try:
                await whitelist.refresh(client)
            except Exception:
                log.exception("Folder refresh failed")
            try:
                await _enforce_storage(conn)
            except Exception:
                log.exception("Storage limit check failed")

    maintenance_task = asyncio.create_task(_maintenance())

    shutdown_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    async with bot_app:
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        _print_status("Control bot", "polling", True)

        # populate the native command menu (the "Menu" button / "/" autocomplete)
        await setup_commands(bot_app)

        # resolve the bot's own dialog so the user client can forward alerts there
        try:
            bot_me = await bot_app.bot.get_me()
            bot_id = bot_me.id  # DM with the bot has this as its chat_id; skip self
            notify_bot = bot_app.bot  # bot sends the alert text → real push
            notify_target = await client.get_input_entity(f"@{bot_me.username}")
            rules = await notifications.get_active_rules(conn)
            _print_status("Notifications", f"{len(rules)} active rules", True)
        except Exception:
            log.warning("Could not resolve bot entity; notifications disabled until restart")
            _print_status("Notifications", "unavailable", False)

        print(f"\n  {C_GREEN}{C_BOLD}Ready.{C_RESET} {C_DIM}Press Ctrl+C to stop.{C_RESET}\n")
        print(f"  {C_DIM}{'─' * 50}{C_RESET}\n")

        try:
            await shutdown_event.wait()
        except KeyboardInterrupt:
            pass

        print(f"\n  {C_DIM}{'─' * 50}{C_RESET}")
        log.info("Shutting down...")
        maintenance_task.cancel()
        await bot_app.updater.stop()
        await bot_app.stop()

    await client.disconnect()
    await conn.close()
    print(f"  {C_YELLOW}Stopped.{C_RESET}\n")


def run() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
