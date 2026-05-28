from __future__ import annotations

import aiosqlite

from app.config import settings

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

CREATE TABLE IF NOT EXISTS ingest_state (
    chat_id INTEGER PRIMARY KEY,
    chat_title TEXT,
    chat_type TEXT,
    last_backfilled_message_id INTEGER,
    last_seen_message_id INTEGER,
    backfill_status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backfill_days (
    date_local TEXT PRIMARY KEY,
    messages_saved INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    completed_at_utc TEXT NOT NULL
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
        # backfill missing chat_titles in ingest_state from messages table
        await conn.execute("""
            UPDATE ingest_state
            SET chat_title = (
                SELECT m.chat_title FROM messages m
                WHERE m.chat_id = ingest_state.chat_id
                  AND m.chat_title IS NOT NULL AND length(m.chat_title) > 0
                LIMIT 1
            )
            WHERE chat_title IS NULL OR length(chat_title) = 0
        """)
        await conn.commit()
    finally:
        await conn.close()
