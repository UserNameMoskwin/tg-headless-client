from __future__ import annotations

import aiosqlite


async def get_backfill_status(conn: aiosqlite.Connection) -> list[dict]:
    rows = await conn.execute_fetchall(
        """
        SELECT chat_id, chat_title, chat_type, backfill_status,
               last_backfilled_message_id, last_error, updated_at_utc
        FROM ingest_state
        ORDER BY updated_at_utc DESC
        """
    )
    return [
        {
            "chat_id": r[0],
            "chat_title": r[1],
            "chat_type": r[2],
            "status": r[3],
            "last_message_id": r[4],
            "last_error": r[5],
            "updated_at": r[6],
        }
        for r in rows
    ]


async def get_db_stats(conn: aiosqlite.Connection) -> dict:
    rows = await conn.execute_fetchall("SELECT COUNT(*) FROM messages")
    total = rows[0][0] if rows else 0

    rows = await conn.execute_fetchall(
        "SELECT MAX(message_date_utc) FROM messages"
    )
    last_msg = rows[0][0] if rows and rows[0][0] else "N/A"

    return {"total_messages": total, "last_message_utc": last_msg}


async def get_backfill_days_stats(conn: aiosqlite.Connection) -> dict:
    rows = await conn.execute_fetchall(
        "SELECT COUNT(*) FROM backfill_days WHERE status = 'completed'"
    )
    days_completed = rows[0][0] if rows else 0
    return {"days_completed": days_completed}
