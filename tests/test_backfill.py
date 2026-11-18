from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from telethon.tl.types import Message, PeerChat

from app.backfill import backfill_chat

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _msg(msg_id: int, days_ago: int) -> Message:
    return Message(id=msg_id, peer_id=PeerChat(77), date=NOW - timedelta(days=days_ago),
                   message=f"m{msg_id}", out=False)


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_messages(self, entity):
        for m in self.messages:  # newest first, like Telegram
            yield m


@pytest.mark.asyncio
async def test_backfill_stops_at_cutoff_and_is_idempotent(db_conn):
    client = _FakeClient([_msg(3, 1), _msg(2, 10), _msg(1, 40)])
    cutoff = NOW - timedelta(days=30)

    assert await backfill_chat(db_conn, client, None, "Chat", cutoff) == (2, 2)
    assert await backfill_chat(db_conn, client, None, "Chat", cutoff) == (2, 0)

    rows = await db_conn.execute_fetchall(
        "SELECT telegram_message_id, chat_title FROM messages ORDER BY telegram_message_id")
    assert [tuple(r) for r in rows] == [(2, "Chat"), (3, "Chat")]
