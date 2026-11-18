"""Storage budget: keep the SQLite DB under a size limit.

Over the limit, whole local days are pruned oldest-first until usage drops to
`target_ratio` of the limit (hysteresis, so we don't prune on every message).
SQLite reuses freed pages, so the file stops growing at about the limit
without a VACUUM.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from app.util import day_bounds_utc

log = logging.getLogger(__name__)


async def _pragma(conn: aiosqlite.Connection, name: str) -> int:
    rows = await conn.execute_fetchall(f"PRAGMA {name}")
    return int(rows[0][0])


async def storage_used_bytes(conn: aiosqlite.Connection) -> int:
    """Bytes held by live data (free pages don't count: SQLite reuses them)."""
    pages = await _pragma(conn, "page_count") - await _pragma(conn, "freelist_count")
    return pages * await _pragma(conn, "page_size")


async def enforce_storage_limit(
    conn: aiosqlite.Connection,
    max_bytes: int,
    tz: ZoneInfo,
    target_ratio: float = 0.9,
) -> dict:
    """Prune the oldest local days while usage exceeds `max_bytes`.

    Returns {"used_before", "used_after", "days_deleted": [YYYY-MM-DD], "messages_deleted"}.
    """
    used = await storage_used_bytes(conn)
    result = {"used_before": used, "used_after": used, "days_deleted": [], "messages_deleted": 0}
    if max_bytes <= 0 or used <= max_bytes:
        return result

    target = int(max_bytes * target_ratio)
    while used > target:
        rows = await conn.execute_fetchall("SELECT MIN(message_date_utc) FROM messages")
        oldest = rows[0][0] if rows else None
        if oldest is None:
            break
        day = datetime.fromisoformat(oldest).astimezone(tz).date().isoformat()
        _, end = day_bounds_utc(day, tz)
        cursor = await conn.execute("DELETE FROM messages WHERE message_date_utc < ?", (end,))
        await conn.commit()
        result["messages_deleted"] += cursor.rowcount
        result["days_deleted"].append(day)
        used = await storage_used_bytes(conn)

    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result["used_after"] = used
    log.info(
        "Storage limit: pruned %d day(s) (%d messages), %.1f → %.1f MB",
        len(result["days_deleted"]), result["messages_deleted"],
        result["used_before"] / 2**20, used / 2**20,
    )
    return result
