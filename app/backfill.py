from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

from app.config import settings
from app.daily_log import append_message
from app.ingest import save_message

log = logging.getLogger(__name__)

FLOOD_WAIT_EXTRA = 5
FILTER_PAUSE = 0.15
CONCURRENT_DIALOGS = 5


def _day_range(days: int, tz: ZoneInfo) -> list[str]:
    """Return list of date strings (YYYY-MM-DD) for the last N days in local tz, newest first."""
    today = datetime.now(tz).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def _date_range_utc_bounds(days: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC bounds covering the last N local days: [oldest_start, newest_end)."""
    today = datetime.now(tz).date()
    oldest = today - timedelta(days=days - 1)
    start = datetime.combine(oldest, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    return start, end


def _msg_local_date(msg: Message, tz: ZoneInfo) -> str:
    """Return the local date string for a message."""
    return msg.date.astimezone(tz).strftime("%Y-%m-%d")


async def get_completed_days(conn: aiosqlite.Connection) -> set[str]:
    rows = await conn.execute_fetchall(
        "SELECT date_local FROM backfill_days WHERE status = 'completed'"
    )
    return {r[0] for r in rows}


async def _mark_days_completed(
    conn: aiosqlite.Connection,
    day_counts: dict[str, int],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
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
            (date_str, count, now),
        )
    await conn.commit()


async def _update_ingest_state(
    conn: aiosqlite.Connection,
    chat_id: int,
    chat_title: str | None,
    chat_type: str | None,
    status: str = "completed",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO ingest_state (chat_id, chat_title, chat_type, backfill_status, updated_at_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            chat_title = excluded.chat_title,
            backfill_status = excluded.backfill_status,
            updated_at_utc = excluded.updated_at_utc
        """,
        (chat_id, chat_title, chat_type, status, now),
    )
    await conn.commit()


def _dialog_type(dialog) -> str:
    return type(dialog.entity).__name__


def _dialog_title(dialog) -> str:
    """Get a human-readable title for any dialog type."""
    if dialog.title:
        return dialog.title
    if dialog.name:
        return dialog.name
    entity = dialog.entity
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or str(dialog.id)


async def _backfill_dialog(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    dialog,
    start_utc: datetime,
    end_utc: datetime,
    pending_days: set[str],
    tz: ZoneInfo,
    sem: asyncio.Semaphore,
    db_lock: asyncio.Lock,
) -> dict[str, int]:
    """Backfill one dialog for the date range. Returns {date: count} of saved messages."""
    chat_title = _dialog_title(dialog)
    day_counts: dict[str, int] = {}
    saved = 0
    batch: list[tuple[Message, str | None]] = []

    async with sem:
        try:
            async for msg in client.iter_messages(dialog, offset_date=end_utc):
                if not isinstance(msg, Message):
                    continue
                if msg.date < start_utc:
                    break

                local_date = _msg_local_date(msg, tz)
                if local_date not in pending_days:
                    continue

                batch.append((msg, chat_title))

        except FloodWaitError as e:
            wait = e.seconds + FLOOD_WAIT_EXTRA
            log.warning("FloodWait %ds for %s, sleeping %ds", e.seconds, chat_title, wait)
            await asyncio.sleep(wait)

        except Exception:
            log.exception("Error backfilling %s", chat_title)

    if batch:
        async with db_lock:
            for msg, ct in batch:
                inserted = await save_message(conn, msg, chat_title=ct)
                if inserted:
                    saved += 1
                    local_date = _msg_local_date(msg, tz)
                    day_counts[local_date] = day_counts.get(local_date, 0) + 1
            if saved > 0:
                await conn.commit()
        from app.daily_log import append_messages_batch
        append_messages_batch(batch, tz)

    return day_counts


async def run_backfill(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    days: int,
    tz: ZoneInfo,
    progress_callback=None,
) -> dict:
    """Backfill last N days by iterating dialogs once.

    Returns {"total_saved": int, "days_processed": int, "days_skipped": int, "dialogs_scanned": int}.
    """
    all_days = set(_day_range(days, tz))
    completed = await get_completed_days(conn)
    pending_days = all_days - completed

    if not pending_days:
        return {"total_saved": 0, "days_processed": 0, "days_skipped": len(all_days), "dialogs_scanned": 0}

    start_utc, end_utc = _date_range_utc_bounds(days, tz)

    all_dialogs = []
    async for dialog in client.iter_dialogs():
        all_dialogs.append(dialog)

    if settings.skip_archived:
        non_archived = [d for d in all_dialogs if not d.archived]
        skipped_archived = len(all_dialogs) - len(non_archived)
    else:
        non_archived = all_dialogs
        skipped_archived = 0

    known_rows = await conn.execute_fetchall(
        "SELECT chat_id, backfill_status FROM ingest_state"
    )
    known_map = {r[0]: r[1] for r in known_rows}

    allowed_ids = set(settings.allowed_telegram_user_ids)
    dialogs = []
    skipped_no_user = 0
    skipped_cached = 0
    need_filter = []

    for dialog in non_archived:
        entity_type = type(dialog.entity).__name__
        if entity_type == "User":
            dialogs.append(dialog)
        elif dialog.id in known_map:
            if known_map[dialog.id] == "completed":
                dialogs.append(dialog)
                skipped_cached += 1
            else:
                skipped_no_user += 1
        else:
            need_filter.append(dialog)

    total_to_filter = len(need_filter)
    if total_to_filter > 0:
        if progress_callback:
            await progress_callback("filtering", 0, total_to_filter, skipped_cached, skipped_archived)

        filter_step = max(1, total_to_filter // 5)
        log.info("Filtering %d new dialogs (%d cached)...", total_to_filter, skipped_cached)

        filter_sem = asyncio.Semaphore(CONCURRENT_DIALOGS)
        filter_done = 0

        async def _check_dialog(dialog):
            nonlocal filter_done, skipped_no_user
            has_user_msg = False
            async with filter_sem:
                try:
                    async for msg in client.iter_messages(dialog, from_user=list(allowed_ids), limit=1):
                        has_user_msg = True
                except Exception:
                    pass

            chat_title = _dialog_title(dialog)
            entity_type = type(dialog.entity).__name__
            if has_user_msg:
                dialogs.append(dialog)
                await _update_ingest_state(conn, dialog.id, chat_title, entity_type)
            else:
                skipped_no_user += 1
                await _update_ingest_state(conn, dialog.id, chat_title, entity_type, status="no_user_messages")

            filter_done += 1
            if progress_callback and filter_done % filter_step == 0 and filter_done < total_to_filter:
                await progress_callback("filtering_progress", filter_done, total_to_filter, len(dialogs), skipped_no_user)

        await asyncio.gather(*[_check_dialog(d) for d in need_filter])
    else:
        log.info("All %d dialogs cached, no filtering needed", skipped_cached)

    log.info(
        "Backfill: %d days pending, %d dialogs to scan (%d cached, %d archived, %d no user messages — skipped)",
        len(pending_days), len(dialogs), skipped_cached, skipped_archived, skipped_no_user,
    )

    total_saved = 0
    all_day_counts: dict[str, int] = {}
    total_dialogs = len(dialogs)
    milestone_step = max(1, total_dialogs // 5)

    if progress_callback:
        await progress_callback("start", 0, total_dialogs, 0, skipped_archived + skipped_no_user)

    sem = asyncio.Semaphore(CONCURRENT_DIALOGS)
    db_lock = asyncio.Lock()
    done_count = 0

    async def _process_dialog(dialog):
        nonlocal done_count, total_saved
        day_counts = await _backfill_dialog(
            client, conn, dialog, start_utc, end_utc, pending_days, tz, sem, db_lock,
        )
        dialog_saved = sum(day_counts.values())

        async with db_lock:
            total_saved += dialog_saved
            for d, c in day_counts.items():
                all_day_counts[d] = all_day_counts.get(d, 0) + c
            await _update_ingest_state(conn, dialog.id, _dialog_title(dialog), _dialog_type(dialog))
            done_count += 1

        if dialog_saved > 0:
            log.info("Dialog %s: %d messages", _dialog_title(dialog), dialog_saved)

        if progress_callback and done_count % milestone_step == 0 and done_count < total_dialogs:
            await progress_callback("progress", done_count, total_dialogs, total_saved, 0)

    await asyncio.gather(*[_process_dialog(d) for d in dialogs])

    await _mark_days_completed(conn, {d: all_day_counts.get(d, 0) for d in pending_days})

    from app.daily_log import rebuild_day_from_db
    for date_str in sorted(pending_days):
        await rebuild_day_from_db(conn, date_str, tz)

    log.info(
        "Backfill complete: %d messages, %d dialogs, %d days",
        total_saved, len(dialogs), len(pending_days),
    )
    return {
        "total_saved": total_saved,
        "days_processed": len(pending_days),
        "days_skipped": len(completed & all_days),
        "dialogs_scanned": len(dialogs),
        "dialogs_skipped_archived": skipped_archived,
        "dialogs_skipped_no_user": skipped_no_user,
    }
