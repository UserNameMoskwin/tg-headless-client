from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.services.stats import _day_boundaries_utc, get_daily_stats, get_daily_stats_by_chat
from tests.conftest import insert_test_message

TZ_TBILISI = ZoneInfo("Asia/Tbilisi")


class TestDayBoundaries:
    def test_tbilisi_boundaries(self):
        start, end = _day_boundaries_utc("2026-05-26", TZ_TBILISI)
        assert "2026-05-25T20:00:00" in start
        assert "2026-05-26T20:00:00" in end

    def test_utc_boundaries(self):
        start, end = _day_boundaries_utc("2026-05-26", ZoneInfo("UTC"))
        assert "2026-05-26T00:00:00" in start
        assert "2026-05-27T00:00:00" in end


@pytest.mark.asyncio
class TestDailyStats:
    async def test_empty_db(self, db_conn):
        result = await get_daily_stats(db_conn, "2026-05-26", TZ_TBILISI)
        assert result["total"] == 0
        assert result["incoming"] == 0
        assert result["outgoing"] == 0

    async def test_counts_incoming_outgoing(self, db_conn):
        await insert_test_message(db_conn, msg_id=1, is_outgoing=0, date_utc="2026-05-26T10:00:00+00:00")
        await insert_test_message(db_conn, msg_id=2, is_outgoing=0, date_utc="2026-05-26T10:01:00+00:00")
        await insert_test_message(db_conn, msg_id=3, is_outgoing=1, date_utc="2026-05-26T10:02:00+00:00")

        result = await get_daily_stats(db_conn, "2026-05-26", TZ_TBILISI)
        assert result["total"] == 3
        assert result["incoming"] == 2
        assert result["outgoing"] == 1

    async def test_excludes_other_dates(self, db_conn):
        await insert_test_message(db_conn, msg_id=1, date_utc="2026-05-26T10:00:00+00:00")
        await insert_test_message(db_conn, msg_id=10, date_utc="2026-05-25T10:00:00+00:00")
        await insert_test_message(db_conn, msg_id=11, date_utc="2026-05-27T10:00:00+00:00")

        result = await get_daily_stats(db_conn, "2026-05-26", TZ_TBILISI)
        assert result["total"] == 1


@pytest.mark.asyncio
class TestDailyStatsByChat:
    async def test_groups_by_chat(self, db_conn):
        await insert_test_message(db_conn, msg_id=20, chat_id=200, chat_title="Chat A", date_utc="2026-05-26T10:00:00+00:00")
        await insert_test_message(db_conn, msg_id=21, chat_id=201, chat_title="Chat B", date_utc="2026-05-26T10:00:00+00:00")
        await insert_test_message(db_conn, msg_id=22, chat_id=200, chat_title="Chat A", date_utc="2026-05-26T11:00:00+00:00")

        rows = await get_daily_stats_by_chat(db_conn, "2026-05-26", TZ_TBILISI)
        by_chat = {r["chat_id"]: r for r in rows}
        assert by_chat[200]["total"] == 2
        assert by_chat[201]["total"] == 1
