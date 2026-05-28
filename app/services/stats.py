from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiosqlite


def _day_boundaries_utc(date_str: str, tz: ZoneInfo) -> tuple[str, str]:
    """Convert a local date string to UTC start/end ISO timestamps."""
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    local_start = datetime.combine(day, time.min, tzinfo=tz)
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return local_start.astimezone(ZoneInfo("UTC")).isoformat(), local_end.astimezone(ZoneInfo("UTC")).isoformat()


async def get_daily_stats(
    conn: aiosqlite.Connection,
    date_str: str,
    tz: ZoneInfo,
) -> dict:
    start_utc, end_utc = _day_boundaries_utc(date_str, tz)
    row = await conn.execute_fetchall(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_outgoing = 0 THEN 1 ELSE 0 END) AS incoming,
            SUM(CASE WHEN is_outgoing = 1 THEN 1 ELSE 0 END) AS outgoing
        FROM messages
        WHERE message_date_utc >= ? AND message_date_utc < ?
        """,
        (start_utc, end_utc),
    )
    r = row[0] if row else (0, 0, 0)
    return {
        "date": date_str,
        "timezone": str(tz),
        "total": r[0] or 0,
        "incoming": r[1] or 0,
        "outgoing": r[2] or 0,
    }


async def get_daily_stats_by_chat(
    conn: aiosqlite.Connection,
    date_str: str,
    tz: ZoneInfo,
) -> list[dict]:
    start_utc, end_utc = _day_boundaries_utc(date_str, tz)
    rows = await conn.execute_fetchall(
        """
        SELECT
            chat_id,
            chat_title,
            COUNT(*) AS total,
            SUM(CASE WHEN is_outgoing = 0 THEN 1 ELSE 0 END) AS incoming,
            SUM(CASE WHEN is_outgoing = 1 THEN 1 ELSE 0 END) AS outgoing
        FROM messages
        WHERE message_date_utc >= ? AND message_date_utc < ?
        GROUP BY chat_id
        ORDER BY total DESC
        """,
        (start_utc, end_utc),
    )
    return [
        {
            "chat_id": r[0],
            "chat_title": r[1],
            "total": r[2],
            "incoming": r[3] or 0,
            "outgoing": r[4] or 0,
        }
        for r in rows
    ]
