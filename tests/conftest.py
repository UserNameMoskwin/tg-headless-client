from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = aiosqlite.Row

    schema = """
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
    await conn.executescript(schema)
    await conn.commit()
    yield conn
    await conn.close()


async def insert_test_message(
    conn: aiosqlite.Connection,
    *,
    msg_id: int = 1,
    chat_id: int = 100,
    chat_title: str = "Test Chat",
    is_outgoing: int = 0,
    date_utc: str = "2026-05-26T12:00:00+00:00",
    text: str = "hello",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (telegram_message_id, chat_id, chat_title, sender_id, sender_name,
             is_outgoing, message_date_utc, text, has_media, raw_json, inserted_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?)
        """,
        (msg_id, chat_id, chat_title, 999, "Sender", is_outgoing, date_utc, text, now),
    )
    await conn.commit()
