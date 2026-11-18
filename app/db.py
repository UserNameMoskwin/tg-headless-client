from __future__ import annotations

import aiosqlite

from app.config import settings

INSERT_MESSAGE = """
INSERT OR IGNORE INTO messages
    (telegram_message_id, chat_id, chat_title, sender_id, sender_name,
     is_outgoing, message_date_utc, text, has_media, media_type,
     raw_json, inserted_at_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_title TEXT,
    sender_id INTEGER,
    sender_name TEXT,
    is_outgoing INTEGER NOT NULL,
    message_date_utc TEXT NOT NULL,
    text TEXT,
    has_media INTEGER DEFAULT 0,
    media_type TEXT,
    raw_json TEXT,
    inserted_at_utc TEXT NOT NULL,
    UNIQUE(chat_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_date_utc ON messages(message_date_utc);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_messages_is_outgoing ON messages(is_outgoing);

CREATE TABLE IF NOT EXISTS notification_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    include_archived INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL
);
"""


async def get_connection() -> aiosqlite.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(settings.db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = aiosqlite.Row
    return conn


async def init_db() -> None:
    conn = await get_connection()
    try:
        await conn.executescript(_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()
