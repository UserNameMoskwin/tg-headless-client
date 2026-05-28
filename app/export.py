from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from app.config import settings

log = logging.getLogger(__name__)

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "exports"


def _date_range_utc(days: int, tz: ZoneInfo) -> tuple[str, str]:
    """Return (start_utc_iso, end_utc_iso) covering the last N local days."""
    today = datetime.now(tz).date()
    oldest = today - timedelta(days=days - 1)
    start = datetime.combine(oldest, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()
    return start, end


async def export_my_messages(
    conn: aiosqlite.Connection,
    days: int,
    tz: ZoneInfo,
) -> dict:
    """Export user's outgoing messages for the last N days from the DB.

    Returns {"file": str, "filename": str, "total": int, "days": int, "chats": int}.
    """
    user_ids = settings.allowed_telegram_user_ids
    start_utc, end_utc = _date_range_utc(days, tz)

    # build query: outgoing messages from allowed users in date range
    placeholders = ",".join("?" for _ in user_ids)
    params: list = list(user_ids) + [start_utc, end_utc]

    query = f"""
        SELECT telegram_message_id, chat_id, chat_title, sender_id, sender_name,
               message_date_utc, text, has_media, media_type
        FROM messages
        WHERE sender_id IN ({placeholders})
          AND message_date_utc >= ?
          AND message_date_utc < ?
        ORDER BY message_date_utc
    """

    rows = await conn.execute_fetchall(query, params)

    # convert to export records
    chats = set()
    records = []
    for r in rows:
        text = r[6]
        if not text:
            continue

        chat_title = r[2]
        if chat_title:
            chats.add(chat_title)

        local_dt = datetime.fromisoformat(r[5]).astimezone(tz)
        records.append({
            "message_id": r[0],
            "chat_id": r[1],
            "chat_title": chat_title,
            "sender_name": r[4],
            "date_utc": r[5],
            "date_local": local_dt.isoformat(),
            "text": text,
            "has_media": bool(r[7]),
            "media_type": r[8],
        })

    # write file
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    filename = f"my_messages_{today}_{days}d.jsonl"
    path = EXPORTS_DIR / filename

    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    log.info("Exported %d messages to %s", len(records), path)
    return {
        "file": str(path),
        "filename": filename,
        "total": len(records),
        "chats": len(chats),
        "days": days,
    }
