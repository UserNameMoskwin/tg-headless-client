from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import aiosqlite
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.guards import is_allowed
from app.config import settings
from app.services import jobs, messages, stats

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_backfill_pending: dict[int, tuple[float, int]] = {}
_backfill_running = False
CONFIRM_TIMEOUT = 120


def _get_conn(context: ContextTypes.DEFAULT_TYPE) -> aiosqlite.Connection:
    return context.bot_data["db_conn"]


def _get_client(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["tg_client"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Telegram Ingestion Server\n\n"
        "/status — server status\n"
        "/stats YYYY-MM-DD — daily stats\n"
        "/stats_by_chat YYYY-MM-DD — stats by chat\n"
        "/dialogs — known dialogs\n"
        "/search query — search messages\n"
        "/backfill N — backfill last N days\n"
        "/exportusershistory N — export your messages (N days)"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    conn = _get_conn(context)
    client = _get_client(context)

    db_info = await jobs.get_db_stats(conn)
    connected = client.is_connected() if client else False

    db_size_mb = 0.0
    try:
        db_size_mb = os.path.getsize(settings.db_path) / (1024 * 1024)
    except OSError:
        pass

    backfill_info = await jobs.get_backfill_days_stats(conn)

    text = (
        "Server status\n\n"
        f"User client: {'connected' if connected else 'disconnected'}\n"
        f"DB size: {db_size_mb:.1f} MB\n"
        f"Messages indexed: {db_info['total_messages']}\n"
        f"Last message: {db_info['last_message_utc']}\n"
        f"Days backfilled: {backfill_info['days_completed']}"
    )
    await update.message.reply_text(text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if not context.args or not _DATE_RE.match(context.args[0]):
        await update.message.reply_text("Usage: /stats YYYY-MM-DD")
        return

    conn = _get_conn(context)
    result = await stats.get_daily_stats(conn, context.args[0], settings.tz)
    text = (
        f"Статистика за {result['date']} ({result['timezone']})\n\n"
        f"Всего: {result['total']}\n"
        f"Входящих: {result['incoming']}\n"
        f"Исходящих: {result['outgoing']}"
    )
    await update.message.reply_text(text)


async def cmd_stats_by_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if not context.args or not _DATE_RE.match(context.args[0]):
        await update.message.reply_text("Usage: /stats_by_chat YYYY-MM-DD")
        return

    conn = _get_conn(context)
    rows = await stats.get_daily_stats_by_chat(conn, context.args[0], settings.tz)
    if not rows:
        await update.message.reply_text("No messages for this date.")
        return

    lines = [f"По чатам за {context.args[0]}:\n"]
    for r in rows[:30]:
        lines.append(f"  {r['chat_title'] or r['chat_id']}: {r['total']} (in:{r['incoming']} out:{r['outgoing']})")
    if len(rows) > 30:
        lines.append(f"\n... and {len(rows) - 30} more chats")
    await update.message.reply_text("\n".join(lines))


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    conn = _get_conn(context)
    rows = await conn.execute_fetchall(
        """
        SELECT i.chat_id, i.chat_title, i.chat_type, COUNT(m.id) as msg_count
        FROM ingest_state i
        LEFT JOIN messages m ON m.chat_id = i.chat_id
        WHERE i.chat_title NOT LIKE '#%'
          AND i.backfill_status != 'no_user_messages'
        GROUP BY i.chat_id
        ORDER BY msg_count DESC
        LIMIT 50
        """
    )
    hidden = await conn.execute_fetchall(
        "SELECT COUNT(*) FROM ingest_state WHERE chat_title LIKE '#%'"
    )
    total = await conn.execute_fetchall("SELECT COUNT(*) FROM ingest_state")
    if not rows:
        await update.message.reply_text("No dialogs indexed yet.")
        return

    lines = [f"Dialogs ({total[0][0]} total, {hidden[0][0]} inactive):\n"]
    for r in rows:
        cnt = r[3]
        lines.append(f"  {r[1]} ({r[2]}) — {cnt} msgs")
    if len(rows) == 50:
        lines.append(f"\n... and more")
    await update.message.reply_text("\n".join(lines))


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /search query text")
        return

    conn = _get_conn(context)
    query = " ".join(context.args)
    results = await messages.search_messages(conn, query, tz=settings.tz, limit=10)
    if not results:
        await update.message.reply_text("Nothing found.")
        return

    lines = [f"Search: {query}\n"]
    for r in results:
        direction = "→" if r["outgoing"] else "←"
        sender = r["sender"] or "?"
        lines.append(f"  {direction} [{r['chat_title']}] {sender}: {r['text'] or '(media)'}")
    await update.message.reply_text("\n".join(lines))


async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _backfill_running
    if not is_allowed(update):
        return

    if _backfill_running:
        await update.message.reply_text("Backfill is already running.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /backfill N\nExample: /backfill 7 — last 7 days")
        return

    try:
        days = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /backfill N (number of days)")
        return

    if days < 1 or days > 10000:
        await update.message.reply_text("Specify 1–10000 days.")
        return

    conn = _get_conn(context)

    from app.backfill import get_completed_days, _day_range
    all_days = _day_range(days, settings.tz)
    completed = await get_completed_days(conn)
    pending = [d for d in all_days if d not in completed]

    if not pending:
        await update.message.reply_text(
            f"All {days} days already backfilled. Nothing to do."
        )
        return

    text = (
        f"Backfill: {len(pending)} days to process, {len(all_days) - len(pending)} already done.\n"
        f"Range: {pending[-1]} → {pending[0]}\n\n"
        f"Send /confirm_backfill to start."
    )
    _backfill_pending[update.effective_user.id] = (
        datetime.now(timezone.utc).timestamp(),
        days,
    )
    await update.message.reply_text(text)


async def cmd_confirm_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _backfill_running
    if not is_allowed(update):
        return

    if _backfill_running:
        await update.message.reply_text("Backfill is already running.")
        return

    uid = update.effective_user.id
    pending = _backfill_pending.pop(uid, None)
    if not pending or (datetime.now(timezone.utc).timestamp() - pending[0]) > CONFIRM_TIMEOUT:
        await update.message.reply_text("No pending backfill request. Use /backfill N first.")
        return

    days = pending[1]
    conn = _get_conn(context)
    client = _get_client(context)

    _backfill_running = True
    asyncio.create_task(_run_backfill_task(client, conn, days, update))


async def _run_backfill_task(client, conn, days: int, update: Update) -> None:
    global _backfill_running
    try:
        from app.backfill import run_backfill

        async def on_progress(stage, done, total, count_a, count_b):
            if stage == "filtering":
                await update.message.reply_text(
                    f"Filtering {total} dialogs ({count_a} archived skipped)..."
                )
            elif stage == "filtering_progress":
                pct = done * 100 // total
                await update.message.reply_text(
                    f"Filtering: {pct}% ({done}/{total}) | {count_a} matched, {count_b} skipped"
                )
            elif stage == "start":
                await update.message.reply_text(
                    f"Scanning: {total} dialogs, {days} days\n"
                    f"Skipped: {count_b} (archived + no user msgs)"
                )
            elif stage == "progress":
                pct = done * 100 // total
                await update.message.reply_text(
                    f"Scanning: {pct}% ({done}/{total}) | {count_a} msgs"
                )

        result = await run_backfill(client, conn, days, settings.tz, progress_callback=on_progress)
        await update.message.reply_text(
            f"Backfill done!\n\n"
            f"Dialogs scanned: {result['dialogs_scanned']}\n"
            f"  archived: {result['dialogs_skipped_archived']} skipped\n"
            f"  no user msgs: {result['dialogs_skipped_no_user']} skipped\n"
            f"Days: {result['days_processed']} processed, {result['days_skipped']} skipped\n"
            f"Messages saved: {result['total_saved']}"
        )
    except Exception as e:
        log.exception("Backfill failed")
        await update.message.reply_text(f"Backfill failed: {e}")
    finally:
        _backfill_running = False


# ── export my messages ──────────────────────────────────────────────

async def cmd_exportusershistory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /export N\nExample: /export 30 — last 30 days")
        return

    try:
        days = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /export N (number of days)")
        return

    if days < 1 or days > 10000:
        await update.message.reply_text("Specify 1–10000 days.")
        return

    conn = _get_conn(context)
    try:
        from app.export import export_my_messages
        result = await export_my_messages(conn, days, settings.tz)
        await update.message.reply_text(
            f"Export done!\n\n"
            f"File: {result['filename']}\n"
            f"Messages: {result['total']}\n"
            f"Chats: {result['chats']}\n"
            f"Period: {result['days']} days"
        )
    except Exception as e:
        log.exception("Export failed")
        await update.message.reply_text(f"Export failed: {e}")
