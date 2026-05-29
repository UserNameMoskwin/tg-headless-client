from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

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

from app.bot.bot import create_bot_app
from app.config import settings
from app.daily_log import append_message
from app.db import get_connection, init_db
from app.ingest import save_message
from app.telegram_client import create_client, start_client

log = logging.getLogger(__name__)

# ANSI colors
C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_BLUE = "\033[34m"
C_WHITE = "\033[97m"

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
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ColorFormatter())

    file_handler = logging.FileHandler(log_dir / "tgclient.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    logging.basicConfig(level=level, handlers=[console, file_handler])

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telethon.network").setLevel(logging.WARNING)
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)


def _print_status(label: str, value: str, ok: bool = True) -> None:
    check = f"{C_GREEN}OK{C_RESET}" if ok else f"{C_RED}FAIL{C_RESET}"
    print(f"  {C_DIM}{'':>8}{C_RESET}  {C_WHITE}{label:<22}{C_RESET} {value} [{check}]")


async def _run() -> None:
    os.system("")  # enable ANSI on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    _setup_logging()
    print(BANNER)

    await init_db()
    conn = await get_connection()
    _print_status("Database", str(settings.db_path.name), True)

    client = create_client()
    me_id = await start_client(client)
    me = await client.get_me()
    _print_status("Telegram client", f"{me.first_name} (id={me_id})", True)

    msg_count = await conn.execute_fetchall("SELECT COUNT(*) FROM messages")
    _print_status("Messages indexed", str(msg_count[0][0]), True)

    # fix missing chat titles from dialog cache
    empty_rows = await conn.execute_fetchall(
        "SELECT chat_id FROM ingest_state WHERE chat_title IS NULL OR length(chat_title) = 0"
    )
    if empty_rows:
        empty_ids = {row[0] for row in empty_rows}
        fixed = 0
        async for d in client.iter_dialogs():
            if d.id in empty_ids:
                entity = d.entity
                first = getattr(entity, "first_name", "") or ""
                last = getattr(entity, "last_name", "") or ""
                title = d.title or d.name or f"{first} {last}".strip()
                if title:
                    await conn.execute(
                        "UPDATE ingest_state SET chat_title = ? WHERE chat_id = ?",
                        (title, d.id),
                    )
                    await conn.execute(
                        "UPDATE messages SET chat_title = ? WHERE chat_id = ? AND (chat_title IS NULL OR length(chat_title) = 0)",
                        (title, d.id),
                    )
                    fixed += 1
                    empty_ids.discard(d.id)
                if not empty_ids:
                    break
        # remaining unfixable entries — mark with ID so we don't retry
        unfixable = len(empty_ids)
        for cid in empty_ids:
            await conn.execute(
                "UPDATE ingest_state SET chat_title = ? WHERE chat_id = ?",
                (f"#{cid}", cid),
            )
        if fixed or unfixable:
            await conn.commit()
            log.info("Chat titles: %d fixed from dialogs, %d marked as #ID (not found)", fixed, unfixable)

    # cache archived chat IDs at startup for filtering
    _archived_ids: set[int] = set()
    if settings.skip_archived:
        async for d in client.iter_dialogs(archived=True):
            _archived_ids.add(d.id)
        log.info("Cached %d archived chats for filtering", len(_archived_ids))

    @client.on(events.NewMessage)
    async def on_new_message(event):
        msg = event.message
        if not isinstance(msg, Message):
            return

        if settings.skip_archived and msg.chat_id in _archived_ids:
            return

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
        inserted = await save_message(conn, msg, chat_title=chat_title)
        if inserted:
            await conn.commit()
            append_message(msg, chat_title, settings.tz)
            preview = _format_message_preview(msg)
            if msg.out:
                log.info("%s%s>>>%s %s: %s", C_CYAN, C_BOLD, C_RESET, chat_title or "?", preview)
            else:
                s = msg.sender
                if s is None:
                    sender = "?"
                elif hasattr(s, "first_name"):
                    sender = (s.first_name or "") or (getattr(s, "last_name", "") or "") or "?"
                else:
                    sender = chat_title or "?"
                log.info("%s<<<  %s%s | %s: %s", C_MAGENTA, chat_title or "?", C_RESET, sender, preview)

    _print_status("Live ingestion", "listening", True)

    bot_app = create_bot_app(client, conn)

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

        print(f"\n  {C_GREEN}{C_BOLD}Ready.{C_RESET} {C_DIM}Press Ctrl+C to stop.{C_RESET}\n")
        print(f"  {C_DIM}{'─' * 50}{C_RESET}\n")

        try:
            await shutdown_event.wait()
        except KeyboardInterrupt:
            pass

        print(f"\n  {C_DIM}{'─' * 50}{C_RESET}")
        log.info("Shutting down...")
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
