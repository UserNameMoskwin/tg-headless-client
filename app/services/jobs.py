from __future__ import annotations

import aiosqlite


async def get_db_stats(conn: aiosqlite.Connection) -> dict:
    rows = await conn.execute_fetchall("SELECT COUNT(*) FROM messages")
    total = rows[0][0] if rows else 0

    rows = await conn.execute_fetchall(
        "SELECT MAX(message_date_utc) FROM messages"
    )
    last_msg = rows[0][0] if rows and rows[0][0] else "N/A"

    return {"total_messages": total, "last_message_utc": last_msg}

