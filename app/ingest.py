from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite
from telethon.tl.types import Channel, Chat, Message

log = logging.getLogger(__name__)

_INSERT_MESSAGE = """
INSERT OR IGNORE INTO messages
    (telegram_message_id, chat_id, chat_title, sender_id, sender_name,
     is_outgoing, message_date_utc, text, has_media, media_type,
     raw_json, inserted_at_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _media_type(msg: Message) -> str | None:
    if not msg.media:
        return None
    return type(msg.media).__name__


def _sender_name(msg: Message) -> str | None:
    s = msg.sender
    if s is None:
        return None
    if isinstance(s, (Channel, Chat)):
        return None
    first = getattr(s, "first_name", "") or ""
    last = getattr(s, "last_name", "") or ""
    return f"{first} {last}".strip() or None


def _raw_json(msg: Message) -> str:
    try:
        return msg.to_json()
    except Exception:
        return json.dumps({"id": msg.id, "error": "serialization_failed"})


async def save_message(
    conn: aiosqlite.Connection,
    msg: Message,
    chat_title: str | None = None,
) -> bool:
    """Save a single message. Returns True if inserted, False if duplicate."""
    if not isinstance(msg, Message):
        return False

    now_utc = datetime.now(timezone.utc).isoformat()
    msg_date = msg.date.astimezone(timezone.utc).isoformat() if msg.date else now_utc

    chat_id = msg.chat_id or msg.peer_id
    sender_id = msg.sender_id

    try:
        cursor = await conn.execute(
            _INSERT_MESSAGE,
            (
                msg.id,
                chat_id,
                chat_title,
                sender_id,
                _sender_name(msg),
                int(msg.out),
                msg_date,
                msg.text,
                int(bool(msg.media)),
                _media_type(msg),
                _raw_json(msg),
                now_utc,
            ),
        )
        return cursor.rowcount > 0
    except Exception:
        log.exception("Failed to save message %d in chat %s", msg.id, chat_id)
        return False
