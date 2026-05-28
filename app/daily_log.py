from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from telethon.tl.types import Channel, Chat, Message

log = logging.getLogger(__name__)

DAYS_DIR = Path(__file__).resolve().parent.parent / "data" / "days"


def _ensure_dir() -> None:
    DAYS_DIR.mkdir(parents=True, exist_ok=True)


def _msg_to_record(msg: Message, chat_title: str | None, tz: ZoneInfo) -> dict:
    local_dt = msg.date.astimezone(tz)

    s = msg.sender
    if s is None or isinstance(s, (Channel, Chat)):
        sender_name = None
    else:
        first = getattr(s, "first_name", "") or ""
        last = getattr(s, "last_name", "") or ""
        sender_name = f"{first} {last}".strip() or None

    return {
        "message_id": msg.id,
        "chat_id": msg.chat_id or msg.peer_id,
        "chat_title": chat_title,
        "sender_id": msg.sender_id,
        "sender_name": sender_name,
        "outgoing": msg.out,
        "date_utc": msg.date.isoformat(),
        "date_local": local_dt.isoformat(),
        "text": msg.text,
        "has_media": bool(msg.media),
        "media_type": type(msg.media).__name__ if msg.media else None,
    }


def append_message(msg: Message, chat_title: str | None, tz: ZoneInfo) -> None:
    """Append a message to today's JSONL file (by local date)."""
    _ensure_dir()
    local_date = msg.date.astimezone(tz).strftime("%Y-%m-%d")
    path = DAYS_DIR / f"{local_date}.jsonl"

    record = _msg_to_record(msg, chat_title, tz)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("Failed to write to %s", path)


def append_messages_batch(
    messages: list[tuple[Message, str | None]],
    tz: ZoneInfo,
) -> dict[str, int]:
    """Append a batch of messages to their respective daily files.

    Returns {date: count} of messages written.
    """
    _ensure_dir()
    day_counts: dict[str, int] = {}
    buffers: dict[str, list[str]] = {}

    for msg, chat_title in messages:
        local_date = msg.date.astimezone(tz).strftime("%Y-%m-%d")
        record = _msg_to_record(msg, chat_title, tz)
        line = json.dumps(record, ensure_ascii=False) + "\n"

        if local_date not in buffers:
            buffers[local_date] = []
        buffers[local_date].append(line)
        day_counts[local_date] = day_counts.get(local_date, 0) + 1

    for date_str, lines in buffers.items():
        path = DAYS_DIR / f"{date_str}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            log.exception("Failed to write batch to %s", path)

    return day_counts


async def rebuild_day_from_db(
    conn: aiosqlite.Connection,
    date_str: str,
    tz: ZoneInfo,
) -> int:
    """Overwrite a day's JSONL with all messages from the DB for that date."""
    _ensure_dir()
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(ZoneInfo("UTC")).isoformat()
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(ZoneInfo("UTC")).isoformat()

    rows = await conn.execute_fetchall(
        """
        SELECT telegram_message_id, chat_id, chat_title, sender_id, sender_name,
               is_outgoing, message_date_utc, text, has_media, media_type
        FROM messages
        WHERE message_date_utc >= ? AND message_date_utc < ?
        ORDER BY message_date_utc
        """,
        (start, end),
    )

    path = DAYS_DIR / f"{date_str}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            record = {
                "message_id": r[0],
                "chat_id": r[1],
                "chat_title": r[2],
                "sender_id": r[3],
                "sender_name": r[4],
                "outgoing": bool(r[5]),
                "date_utc": r[6],
                "text": r[7],
                "has_media": bool(r[8]),
                "media_type": r[9],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    count = len(rows)
    log.info("Rebuilt %s: %d messages", path.name, count)
    return count
