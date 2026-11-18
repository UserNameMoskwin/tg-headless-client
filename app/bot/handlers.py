from __future__ import annotations

import html
import logging
import os
import re

import aiosqlite
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app.bot.guards import is_allowed
from app.config import settings
from app.services import jobs, notifications, retention

log = logging.getLogger(__name__)


def _get_conn(context: ContextTypes.DEFAULT_TYPE) -> aiosqlite.Connection:
    return context.bot_data["db_conn"]


def _get_client(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["tg_client"]


# ── menu, keyboard & help ───────────────────────────────────────────

BTN_STATUS = "📊 Статус"
BTN_NOTIFY = "🔔 Уведомления"
BTN_HELP = "📖 Команды"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_STATUS, BTN_NOTIFY],
        [BTN_HELP],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Exact texts the persistent keyboard sends; used to route button taps.
BUTTON_LABELS = [BTN_STATUS, BTN_NOTIFY, BTN_HELP]

# (command, description) — single source for /help text and the native
# Telegram command menu (set via setMyCommands on startup).
COMMANDS: list[tuple[str, str]] = [
    ("status", "Статус сервера: подключение, чаты, хранилище, число сообщений"),
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
    elif text == BTN_NOTIFY:
        await cmd_notify(update, context)
    elif text == BTN_HELP:
        await cmd_help(update, context)


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

    used_mb = await retention.storage_used_bytes(conn) / 2**20
    limit = f" / {settings.max_storage_mb} MB" if settings.max_storage_mb > 0 else " (без лимита)"
    whitelist = context.bot_data.get("whitelist")
    chats = whitelist.describe() if whitelist is not None else "все чаты"

    text = (
        "Server status\n\n"
        f"User client: {'connected' if connected else 'disconnected'}\n"
        f"Чаты: {chats}\n"
        f"Хранилище: {used_mb:.1f} MB{limit}\n"
        f"DB file: {db_size_mb:.1f} MB\n"
        f"Messages: {db_info['total_messages']}\n"
        f"Last message: {db_info['last_message_utc']}"
    )
    await update.message.reply_text(text)


# ── notification rules ──────────────────────────────────────────────

# HTML, so literal <id> must be escaped to render.
_NOTIFY_USAGE = (
    "<b>Команды</b>\n"
    "/notify_add — добавить триггер (пошагово)\n"
    "/notify_edit &lt;id&gt; — изменить триггер (пошагово)\n"
    "/notify_on &lt;id&gt; · /notify_off &lt;id&gt; — вкл/выкл\n"
    "/notify_del &lt;id&gt; — удалить триггер"
)

_DIVIDER = "─" * 18


async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    conn = _get_conn(context)
    rules = await notifications.list_rules(conn)
    if not rules:
        await update.message.reply_text(
            "🔔 Триггеров нет.\n\n" + _NOTIFY_USAGE, parse_mode=ParseMode.HTML
        )
        return

    lines = [f"🔔 <b>Уведомления ({len(rules)})</b>", ""]
    for r in rules:
        state = "✅ включён" if r["is_active"] else "⛔ выключен"
        arch = "да" if r["include_archived"] else "нет"
        name = html.escape(r["name"])
        pattern = html.escape(r["pattern"])
        lines.append(f"<b>#{r['id']} · {name}</b>")
        lines.append(f"Статус: {state} · архив: {arch}")
        lines.append(f"<code>{pattern}</code>")
        lines.append("")
    lines.append(_DIVIDER)
    lines.append(_NOTIFY_USAGE)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _parse_rule_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None


def _rule_id_for(cmd: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Id from `/notify_x 5` (args) or the clickable `/notify_x_5` form."""
    rid = _parse_rule_id(context)
    if rid is not None:
        return rid
    m = re.match(rf"^/{cmd}_(\d+)\b", update.message.text or "")
    return int(m.group(1)) if m else None


async def _list_rules_as_commands(
    update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str, prompt: str
) -> None:
    """No-id fallback: list rules as clickable /<cmd>_<id> commands."""
    conn = _get_conn(context)
    rules = await notifications.list_rules(conn)
    if not rules:
        await update.message.reply_text("Триггеров нет. Добавьте через /notify_add.")
        return
    lines = [prompt, ""]
    for r in rules:
        state = "✅" if r["is_active"] else "⛔"
        lines.append(f"{state} {r['name']}")
        lines.append(f"   /{cmd}_{r['id']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_notify_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    rule_id = _rule_id_for("notify_on", update, context)
    if rule_id is None:
        await _list_rules_as_commands(
            update, context, "notify_on", "Какой триггер включить?"
        )
        return
    conn = _get_conn(context)
    if await notifications.set_active(conn, rule_id, True):
        await update.message.reply_text(f"Триггер #{rule_id} включён.")
    else:
        await update.message.reply_text(f"Триггер #{rule_id} не найден.")


async def cmd_notify_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    rule_id = _rule_id_for("notify_off", update, context)
    if rule_id is None:
        await _list_rules_as_commands(
            update, context, "notify_off", "Какой триггер выключить?"
        )
        return
    conn = _get_conn(context)
    if await notifications.set_active(conn, rule_id, False):
        await update.message.reply_text(f"Триггер #{rule_id} выключен.")
    else:
        await update.message.reply_text(f"Триггер #{rule_id} не найден.")


async def cmd_notify_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    rule_id = _rule_id_for("notify_del", update, context)
    if rule_id is None:
        await _list_rules_as_commands(
            update, context, "notify_del", "Какой триггер удалить?"
        )
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
    conn = _get_conn(context)

    rule_id = _rule_id_for("notify_edit", update, context)
    if rule_id is None:
        # no id → list rules as clickable /notify_edit_<id> commands
        await _list_rules_as_commands(
            update, context, "notify_edit", "Выберите триггер для редактирования:"
        )
        return ConversationHandler.END

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
