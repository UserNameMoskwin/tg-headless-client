"""Backfill all history from a Telegram Desktop JSON data export into SQLite.

Usage:
    python -m app.backfill_from_export [path/to/result.json]

Defaults to the latest DataExport_* folder in data/exports/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from app.config import settings
from app.db import get_connection, init_db

log = logging.getLogger(__name__)

BATCH_SIZE = 5000

EXPORT_TYPE_TO_CHAT_TYPE = {
    "personal_chat": "User",
    "bot_chat": "User",
    "saved_messages": "User",
    "private_group": "Chat",
    "private_supergroup": "Channel",
    "public_supergroup": "Channel",
    "private_channel": "Channel",
    "public_channel": "Channel",
}

_INSERT = """
INSERT OR IGNORE INTO messages
    (telegram_message_id, chat_id, chat_title, sender_id, sender_name,
     is_outgoing, message_date_utc, text, has_media, media_type,
     raw_json, inserted_at_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _parse_sender_id(from_id: str | None) -> int | None:
    if not from_id:
        return None
    for prefix in ("user", "channel", "chat"):
        if from_id.startswith(prefix):
            try:
                return int(from_id[len(prefix):])
            except ValueError:
                return None
    return None


def _flatten_text(text) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for part in text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text", ""))
        return "".join(parts)
    return str(text) if text else ""


def _detect_media_type(msg: dict) -> str | None:
    mt = msg.get("media_type")
    if mt:
        return mt
    if msg.get("photo"):
        return "photo"
    if msg.get("file"):
        f = msg["file"]
        if isinstance(f, str) and f != "(File not included. Change data exporting settings to download.)":
            return "document"
    return None


def _msg_date_utc(msg: dict) -> str:
    unix = msg.get("date_unixtime")
    if unix:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc).isoformat()
    raw = msg.get("date", "")
    if raw:
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _find_export_file(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"Not found: {p}")

    exports_dir = Path(__file__).resolve().parent.parent / "data" / "exports"
    candidates = sorted(exports_dir.glob("DataExport_*/result.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No DataExport_*/result.json found in {exports_dir}")
    return candidates[0]


async def run_import(export_path: Path, tz: ZoneInfo) -> dict:
    now_utc = datetime.now(timezone.utc).isoformat()
    user_id = settings.allowed_telegram_user_id

    log.info("Loading %s ...", export_path)
    t0 = time.monotonic()
    with open(export_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    t_load = time.monotonic() - t0
    log.info("Loaded in %.1fs", t_load)

    chats = data.get("chats", {}).get("list", [])
    log.info("Chats in export: %d", len(chats))

    await init_db()
    conn = await get_connection()

    total_inserted = 0
    total_skipped = 0
    total_messages = 0
    chats_processed = 0
    day_counts: dict[str, int] = {}

    async def _flush_batch(conn: aiosqlite.Connection, batch: list[tuple]) -> int:
        before = (await conn.execute_fetchall("SELECT total_changes()"))[0][0]
        await conn.executemany(_INSERT, batch)
        after = (await conn.execute_fetchall("SELECT total_changes()"))[0][0]
        await conn.commit()
        return after - before

    try:
        for chat in chats:
            chat_id = chat.get("id")
            chat_name = chat.get("name")
            chat_type_export = chat.get("type", "")
            chat_type = EXPORT_TYPE_TO_CHAT_TYPE.get(chat_type_export, "Unknown")
            messages_list = chat.get("messages", [])

            if not messages_list:
                continue

            if chat_type_export == "saved_messages":
                chat_name = "Saved Messages"

            batch_params = []
            batch_dates = []
            for msg in messages_list:
                if msg.get("type") != "message":
                    continue

                total_messages += 1
                msg_id = msg.get("id")
                if not msg_id or not chat_id:
                    total_skipped += 1
                    continue

                sender_id = _parse_sender_id(msg.get("from_id"))
                sender_name = msg.get("from")
                is_outgoing = 1 if sender_id == user_id else 0

                text = _flatten_text(msg.get("text", ""))
                date_utc = _msg_date_utc(msg)
                media_type = _detect_media_type(msg)
                has_media = 1 if media_type else 0

                raw = json.dumps(msg, ensure_ascii=False)

                local_date = datetime.fromisoformat(date_utc).astimezone(tz).strftime("%Y-%m-%d")

                batch_params.append((
                    msg_id, chat_id, chat_name, sender_id, sender_name,
                    is_outgoing, date_utc, text, has_media, media_type,
                    raw, now_utc,
                ))
                batch_dates.append(local_date)

                if len(batch_params) >= BATCH_SIZE:
                    inserted = await _flush_batch(conn, batch_params)
                    total_inserted += inserted
                    total_skipped += len(batch_params) - inserted
                    for ld in batch_dates:
                        day_counts[ld] = day_counts.get(ld, 0) + 1
                    batch_params = []
                    batch_dates = []

            if batch_params:
                inserted = await _flush_batch(conn, batch_params)
                total_inserted += inserted
                total_skipped += len(batch_params) - inserted
                for ld in batch_dates:
                    day_counts[ld] = day_counts.get(ld, 0) + 1

            await conn.execute(
                """
                INSERT INTO ingest_state (chat_id, chat_title, chat_type, backfill_status, updated_at_utc)
                VALUES (?, ?, ?, 'completed', ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_title = COALESCE(NULLIF(excluded.chat_title, ''), ingest_state.chat_title),
                    chat_type = excluded.chat_type,
                    backfill_status = 'completed',
                    updated_at_utc = excluded.updated_at_utc
                """,
                (chat_id, chat_name, chat_type, now_utc),
            )
            await conn.commit()

            chats_processed += 1
            if chats_processed % 100 == 0:
                log.info(
                    "Progress: %d/%d chats | %d inserted, %d dupes",
                    chats_processed, len(chats), total_inserted, total_skipped,
                )

        # mark all days as completed in backfill_days
        log.info("Marking %d days as completed...", len(day_counts))
        for date_str, count in day_counts.items():
            await conn.execute(
                """
                INSERT INTO backfill_days (date_local, messages_saved, status, completed_at_utc)
                VALUES (?, ?, 'completed', ?)
                ON CONFLICT(date_local) DO UPDATE SET
                    messages_saved = messages_saved + excluded.messages_saved,
                    status = 'completed',
                    completed_at_utc = excluded.completed_at_utc
                """,
                (date_str, count, now_utc),
            )
        await conn.commit()

        # rebuild daily JSONL logs
        log.info("Rebuilding %d daily logs...", len(day_counts))
        from app.daily_log import rebuild_day_from_db
        days_sorted = sorted(day_counts.keys())
        for i, date_str in enumerate(days_sorted):
            await rebuild_day_from_db(conn, date_str, tz)
            if (i + 1) % 100 == 0:
                log.info("Daily logs: %d/%d", i + 1, len(days_sorted))

    finally:
        await conn.close()

    elapsed = time.monotonic() - t0
    result = {
        "total_messages_in_export": total_messages,
        "inserted": total_inserted,
        "duplicates_skipped": total_skipped,
        "chats_processed": chats_processed,
        "days_covered": len(day_counts),
        "date_range": (days_sorted[0], days_sorted[-1]) if day_counts else None,
        "elapsed_seconds": round(elapsed, 1),
    }
    log.info("Import complete: %s", result)
    return result


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    export_path = _find_export_file(explicit)
    log.info("Using export: %s", export_path)

    result = asyncio.run(run_import(export_path, settings.tz))
    print()
    print("=" * 50)
    print("BACKFILL FROM EXPORT COMPLETE")
    print("=" * 50)
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
