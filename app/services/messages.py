from __future__ import annotations

from zoneinfo import ZoneInfo

import aiosqlite

from app.services.stats import _day_boundaries_utc


async def search_messages(
    conn: aiosqlite.Connection,
    query: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    chat_id: int | None = None,
    tz: ZoneInfo | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = ["text LIKE ?"]
    params: list = [f"%{query}%"]

    if date_from and tz:
        start, _ = _day_boundaries_utc(date_from, tz)
        clauses.append("message_date_utc >= ?")
        params.append(start)

    if date_to and tz:
        _, end = _day_boundaries_utc(date_to, tz)
        clauses.append("message_date_utc < ?")
        params.append(end)

    if chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(chat_id)

    where = " AND ".join(clauses)
    params.append(limit)

    rows = await conn.execute_fetchall(
        f"""
        SELECT telegram_message_id, chat_id, chat_title, sender_name,
               is_outgoing, message_date_utc, text
        FROM messages
        WHERE {where}
        ORDER BY message_date_utc DESC
        LIMIT ?
        """,
        params,
    )
    return [
        {
            "message_id": r[0],
            "chat_id": r[1],
            "chat_title": r[2],
            "sender": r[3],
            "outgoing": bool(r[4]),
            "date_utc": r[5],
            "text": r[6][:200] if r[6] else None,
        }
        for r in rows
    ]
