from __future__ import annotations

import pytest

from tests.conftest import insert_test_message


@pytest.mark.asyncio
class TestIdempotentInsert:
    async def test_no_duplicates(self, db_conn):
        await insert_test_message(db_conn, msg_id=100, chat_id=500)
        await insert_test_message(db_conn, msg_id=100, chat_id=500)

        rows = await db_conn.execute_fetchall(
            "SELECT COUNT(*) FROM messages WHERE telegram_message_id = 100 AND chat_id = 500"
        )
        assert rows[0][0] == 1

    async def test_different_chats_same_msg_id(self, db_conn):
        await insert_test_message(db_conn, msg_id=100, chat_id=600)
        await insert_test_message(db_conn, msg_id=100, chat_id=601)

        rows = await db_conn.execute_fetchall(
            "SELECT COUNT(*) FROM messages WHERE telegram_message_id = 100"
        )
        assert rows[0][0] == 2
