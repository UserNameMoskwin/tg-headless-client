from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import aiosqlite
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from app.bot.guards import is_allowed
from app.config import settings
from app.services import jobs, messages, notifications, stats

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_backfill_pending: dict[int, tuple[float, int]] = {}
_backfill_running = False
CONFIRM_TIMEOUT = 120


def _get_conn(context: ContextTypes.DEFAULT_TYPE) -> aiosqlite.Connection:
    return context.bot_data["db_conn"]


def _get_client(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["tg_client"]


# ── menu, keyboard & help ───────────────────────────────────────────

BTN_STATUS = "📊 Статус"
BTN_STATS = "📅 Статистика"
BTN_DIALOGS = "💬 Диалоги"
BTN_NOTIFY = "🔔 Уведомления"
BTN_HELP = "📖 Команды"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_STATUS, BTN_DIALOGS],
        [BTN_STATS, BTN_NOTIFY],
        [BTN_HELP],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Exact texts the persistent keyboard sends; used to route button taps.
BUTTON_LABELS = [BTN_STATUS, BTN_STATS, BTN_DIALOGS, BTN_NOTIFY, BTN_HELP]

# (command, description) — single source for /help text and the native
# Telegram command menu (set via setMyCommands on startup).
COMMANDS: list[tuple[str, str]] = [
    ("status", "Статус сервера: подключение, размер БД, число сообщений"),
    ("stats", "Статистика за день — /stats ГГГГ-ММ-ДД"),
    ("stats_by_chat", "Статистика по чатам за день — /stats_by_chat ГГГГ-ММ-ДД"),
    ("dialogs", "Список известных диалогов"),
    ("search", "Поиск по сообщениям — /search текст"),
    ("backfill", "Догрузить историю за N дней — /backfill N"),
    ("exportusershistory", "Экспорт ваших сообщений за N дней — /exportusershistory N"),
    ("notify", "Уведомления по ключевым словам (форвард при срабатывании)"),
    ("notify_add", "Добавить триггер уведомлений (пошагово)"),
    ("notify_edit", "Изменить триггер уведомлений — /notify_edit <id>"),
    ("help", "Список команд и описания"),
]


def bot_commands() -> list[BotCommand]:
    return [BotCommand(cmd, desc) for cmd, desc in COMMANDS]


def _help_text() -> str:
    lines = ["Доступные команды:", ""]
    lines += [f"/{cmd} — {desc}" for cmd, desc in COMMANDS]
    lines += ["", "Кнопки под полем ввода — быстрый доступ к основным функциям."]
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Telegram Ingestion Server\n\n"
        "Используйте кнопки ниже или /help — список всех команд.",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(_help_text(), reply_markup=MAIN_KEYBOARD)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a persistent-keyboard tap to the matching command."""
    if not is_allowed(update):
        return
    text = update.message.text
    if text == BTN_STATUS:
        await cmd_status(update, context)
    elif text == BTN_DIALOGS:
        await cmd_dialogs(update, context)
    elif text == BTN_NOTIFY:
        await cmd_notify(update, context)
    elif text == BTN_HELP:
        await cmd_help(update, context)
    elif text == BTN_STATS:
        # no-arg button → today's stats in the local timezone
        context.args = [datetime.now(settings.tz).strftime("%Y-%m-%d")]
        await cmd_stats(update, context)


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
        f"Исходящих: {result['outgoing']}\n\n"
        f"{stats.format_source_note(result['backfilled'], result['live'])}"
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

    summary = await stats.get_daily_stats(conn, context.args[0], settings.tz)
    lines = [
        f"По чатам за {context.args[0]}:",
        stats.format_source_note(summary['backfilled'], summary['live']),
        "",
    ]
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


# ── notification rules ──────────────────────────────────────────────

_NOTIFY_USAGE = (
    "Уведомления по ключевым словам (форвард при срабатывании).\n\n"
    "/notify_add — добавить триггер (пошагово)\n"
    "/notify_edit <id> — изменить триггер (пошагово)\n"
    "/notify_on <id> · /notify_off <id> — вкл/выкл триггер\n"
    "/notify_del <id> — удалить триггер"
)


async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    conn = _get_conn(context)
    rules = await notifications.list_rules(conn)
    if not rules:
        await update.message.reply_text("Триггеров нет.\n\n" + _NOTIFY_USAGE)
        return

    lines = [f"Уведомления ({len(rules)}):", ""]
    for r in rules:
        state = "✅" if r["is_active"] else "⛔"
        arch = "архив:вкл" if r["include_archived"] else "архив:выкл"
        lines.append(f"[#{r['id']}] {state} {r['name']} · 🗄 {arch}")
        lines.append(f"    {r['pattern']}")
    lines.append("")
    lines.append(_NOTIFY_USAGE)
    await update.message.reply_text("\n".join(lines))


def _parse_rule_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None


async def cmd_notify_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    rule_id = _parse_rule_id(context)
    if rule_id is None:
        await update.message.reply_text("Usage: /notify_on <id>")
        return
    conn = _get_conn(context)
    if await notifications.set_active(conn, rule_id, True):
        await update.message.reply_text(f"Триггер #{rule_id} включён.")
    else:
        await update.message.reply_text(f"Триггер #{rule_id} не найден.")


async def cmd_notify_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    rule_id = _parse_rule_id(context)
    if rule_id is None:
        await update.message.reply_text("Usage: /notify_off <id>")
        return
    conn = _get_conn(context)
    if await notifications.set_active(conn, rule_id, False):
        await update.message.reply_text(f"Триггер #{rule_id} выключен.")
    else:
        await update.message.reply_text(f"Триггер #{rule_id} не найден.")


async def cmd_notify_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    rule_id = _parse_rule_id(context)
    if rule_id is None:
        await update.message.reply_text("Usage: /notify_del <id>")
        return
    conn = _get_conn(context)
    if await notifications.delete_rule(conn, rule_id):
        await update.message.reply_text(f"Триггер #{rule_id} удалён.")
    else:
        await update.message.reply_text(f"Триггер #{rule_id} не найден.")


# ── notification add/edit dialog (ConversationHandler) ───────────────
#
# Both /notify_add and /notify_edit drive the same sequential flow:
#   name → keywords → archived-chats (inline) → active (inline) → save.
# Edit pre-loads the rule into context.user_data and shows each current
# value; the user may keep it by tapping "Оставить" / sending "-".
#
# State is per-user in context.user_data["notify_draft"], so a second
# /notify_add restarts cleanly via the entry points.

NOTIFY_NAME, NOTIFY_KEYWORDS, NOTIFY_ARCHIVED, NOTIFY_ACTIVE = range(4)

_KEEP_HINT = "\n\nОтправьте «-», чтобы оставить текущее значение."
_KEYWORDS_HINT = (
    "Слова через запятую (ИЛИ) и плюс (И-связка).\n"
    "Пример: дедлайн, отчёт + срочно\n"
    "Сработает на «дедлайн» ИЛИ («отчёт» И «срочно»).\n"
    "Склонения учитываются: «сделать» поймает «сделай», «сделаем»."
)


def _yes_no_keyboard(prefix: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Да", callback_data=f"{prefix}:yes"),
            InlineKeyboardButton("Нет", callback_data=f"{prefix}:no"),
        ]]
    )


async def cmd_notify_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    context.user_data["notify_draft"] = {"edit_id": None}
    await update.message.reply_text(
        "Новый триггер.\n\nКак назвать? (отправьте /cancel для отмены)"
    )
    return NOTIFY_NAME


async def cmd_notify_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    rule_id = _parse_rule_id(context)
    if rule_id is None:
        await update.message.reply_text("Usage: /notify_edit <id>")
        return ConversationHandler.END

    conn = _get_conn(context)
    rule = await notifications.get_rule(conn, rule_id)
    if not rule:
        await update.message.reply_text(f"Триггер #{rule_id} не найден.")
        return ConversationHandler.END

    context.user_data["notify_draft"] = {
        "edit_id": rule_id,
        "name": rule["name"],
        "pattern": rule["pattern"],
        "is_active": rule["is_active"],
        "include_archived": rule["include_archived"],
    }
    await update.message.reply_text(
        f"Редактирование триггера #{rule_id}.\n\n"
        f"Текущее название: {rule['name']}\n"
        f"Новое название?{_KEEP_HINT}\n\n(/cancel — отмена)"
    )
    return NOTIFY_NAME


def _keep(text: str) -> bool:
    return text.strip() == "-"


async def notify_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["notify_draft"]
    text = update.message.text
    if not (draft["edit_id"] is not None and _keep(text)):
        name = text.strip()
        if not name:
            await update.message.reply_text("Название не может быть пустым. Введите ещё раз:")
            return NOTIFY_NAME
        draft["name"] = name

    if draft["edit_id"] is not None:
        await update.message.reply_text(
            f"Текущие ключи: {draft['pattern']}\n\n"
            f"Новые ключи?\n{_KEYWORDS_HINT}{_KEEP_HINT}"
        )
    else:
        await update.message.reply_text(f"Ключевые слова?\n\n{_KEYWORDS_HINT}")
    return NOTIFY_KEYWORDS


async def notify_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["notify_draft"]
    text = update.message.text
    if not (draft["edit_id"] is not None and _keep(text)):
        pattern = text.strip()
        if not notifications.parse_pattern(pattern):
            await update.message.reply_text(
                "Не вижу ни одного слова. Введите ключи ещё раз:"
            )
            return NOTIFY_KEYWORDS
        draft["pattern"] = pattern

    current = ""
    if draft["edit_id"] is not None:
        current = f" (сейчас: {'да' if draft['include_archived'] else 'нет'})"
    await update.message.reply_text(
        f"Учитывать архивные чаты?{current}",
        reply_markup=_yes_no_keyboard("notify_arch"),
    )
    return NOTIFY_ARCHIVED


async def notify_archived(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = context.user_data["notify_draft"]
    draft["include_archived"] = query.data.endswith(":yes")

    current = ""
    if draft["edit_id"] is not None:
        current = f" (сейчас: {'да' if draft.get('is_active', True) else 'нет'})"
    await query.edit_message_text(
        f"Архивные чаты: {'да' if draft['include_archived'] else 'нет'}"
    )
    await query.message.reply_text(
        f"Включить триггер сразу?{current}",
        reply_markup=_yes_no_keyboard("notify_active"),
    )
    return NOTIFY_ACTIVE


async def notify_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = context.user_data["notify_draft"]
    draft["is_active"] = query.data.endswith(":yes")
    await query.edit_message_text(
        f"Активен: {'да' if draft['is_active'] else 'нет'}"
    )

    conn = _get_conn(context)
    if draft["edit_id"] is not None:
        await notifications.update_rule(
            conn,
            draft["edit_id"],
            name=draft["name"],
            pattern=draft["pattern"],
            is_active=draft["is_active"],
            include_archived=draft["include_archived"],
        )
        rule_id = draft["edit_id"]
        verb = "обновлён"
    else:
        rule_id = await notifications.add_rule(conn, draft["name"], draft["pattern"])
        if not draft["is_active"]:
            await notifications.set_active(conn, rule_id, False)
        if draft["include_archived"]:
            await notifications.set_archived(conn, rule_id, True)
        verb = "добавлен"

    state = "✅ активен" if draft["is_active"] else "⛔ выключен"
    arch = "вкл" if draft["include_archived"] else "выкл"
    await query.message.reply_text(
        f"Триггер #{rule_id} «{draft['name']}» {verb}.\n"
        f"Ключи: {draft['pattern']}\n"
        f"Состояние: {state} · архив: {arch}",
        reply_markup=MAIN_KEYBOARD,
    )
    context.user_data.pop("notify_draft", None)
    return ConversationHandler.END


async def notify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("notify_draft", None)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END
