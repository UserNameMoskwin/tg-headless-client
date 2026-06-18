from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import aiosqlite
import snowballstemmer
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, Message

log = logging.getLogger(__name__)


# ── pattern parsing & matching ───────────────────────────────────────
#
# A rule's `pattern` is a small text spec entered through the bot:
#   - alternatives are separated by ","  (OR)
#   - words inside one alternative are joined by "+"  (AND — a "связка")
#
#   "дедлайн, отчёт + срочно"
#     → fires if text has "дедлайн"  OR  (has "отчёт" AND has "срочно")
#
# Matching is morphology-aware: both the keywords and the message text are
# reduced to stems (Snowball, RU/EN), so a keyword "сделать" also fires on
# "сделай", "сделаем", "сделаю" etc. Stems are compared per word token, not
# as substrings, which avoids accidental hits ("кот" ≠ "который").

_ru_stemmer = snowballstemmer.stemmer("russian")
_en_stemmer = snowballstemmer.stemmer("english")
_CYRILLIC_RE = re.compile(r"[а-яё]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def stem_word(word: str) -> str:
    """Reduce a single word to its stem, choosing RU/EN by script."""
    w = word.lower()
    if _CYRILLIC_RE.search(w):
        return _ru_stemmer.stemWord(w)
    return _en_stemmer.stemWord(w)


def text_to_stems(text: str) -> set[str]:
    """Tokenize text into words and reduce each to its stem."""
    return {stem_word(tok) for tok in _WORD_RE.findall(text.lower())}


def parse_pattern(pattern: str) -> list[list[str]]:
    """Parse a raw pattern into a list of AND-groups (the OR alternatives)."""
    alternatives: list[list[str]] = []
    for alt in pattern.split(","):
        terms = [t.strip().lower() for t in alt.split("+")]
        terms = [t for t in terms if t]
        if terms:
            alternatives.append(terms)
    return alternatives


def compile_pattern(pattern: str) -> list[list[list[str]]]:
    """Compile a pattern to stems: OR-groups → AND-terms → token stems.

    A term may contain several words (e.g. an accidental phrase); all of its
    token stems must be present for the term to match.
    """
    groups: list[list[list[str]]] = []
    for alt in parse_pattern(pattern):
        terms = []
        for term in alt:
            stems = [stem_word(tok) for tok in _WORD_RE.findall(term)]
            if stems:
                terms.append(stems)
        if terms:
            groups.append(terms)
    return groups


def pattern_matches(compiled: list[list[list[str]]], stems: set[str]) -> bool:
    """True if any OR-group has all its AND-terms present in the stem set."""
    return any(
        all(all(s in stems for s in term) for term in group)
        for group in compiled
    )


def match_rules(text: str, rules: list[dict], is_archived: bool) -> list[dict]:
    """Return active rules (pre-compiled) that fire for this text."""
    stems = text_to_stems(text)
    matched = []
    for rule in rules:
        if is_archived and not rule["include_archived"]:
            continue
        if pattern_matches(rule["compiled"], stems):
            matched.append(rule)
    return matched


# ── persistence (CRUD) ───────────────────────────────────────────────


def _row_to_rule(r: aiosqlite.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "pattern": r["pattern"],
        "is_active": bool(r["is_active"]),
        "include_archived": bool(r["include_archived"]),
    }


async def list_rules(conn: aiosqlite.Connection) -> list[dict]:
    rows = await conn.execute_fetchall(
        "SELECT id, name, pattern, is_active, include_archived "
        "FROM notification_rules ORDER BY id"
    )
    return [_row_to_rule(r) for r in rows]


async def add_rule(conn: aiosqlite.Connection, name: str, pattern: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor = await conn.execute(
        "INSERT INTO notification_rules (name, pattern, created_at_utc) "
        "VALUES (?, ?, ?)",
        (name, pattern, now),
    )
    await conn.commit()
    invalidate_cache()
    return cursor.lastrowid


async def set_active(conn: aiosqlite.Connection, rule_id: int, active: bool) -> bool:
    cursor = await conn.execute(
        "UPDATE notification_rules SET is_active = ? WHERE id = ?",
        (int(active), rule_id),
    )
    await conn.commit()
    invalidate_cache()
    return cursor.rowcount > 0


async def set_archived(conn: aiosqlite.Connection, rule_id: int, include: bool) -> bool:
    cursor = await conn.execute(
        "UPDATE notification_rules SET include_archived = ? WHERE id = ?",
        (int(include), rule_id),
    )
    await conn.commit()
    invalidate_cache()
    return cursor.rowcount > 0


async def update_rule(
    conn: aiosqlite.Connection,
    rule_id: int,
    *,
    name: str,
    pattern: str,
    is_active: bool,
    include_archived: bool,
) -> bool:
    """Overwrite all editable fields of a rule in one shot (edit dialog)."""
    cursor = await conn.execute(
        "UPDATE notification_rules "
        "SET name = ?, pattern = ?, is_active = ?, include_archived = ? "
        "WHERE id = ?",
        (name, pattern, int(is_active), int(include_archived), rule_id),
    )
    await conn.commit()
    invalidate_cache()
    return cursor.rowcount > 0


async def get_rule(conn: aiosqlite.Connection, rule_id: int) -> dict | None:
    rows = await conn.execute_fetchall(
        "SELECT id, name, pattern, is_active, include_archived "
        "FROM notification_rules WHERE id = ?",
        (rule_id,),
    )
    return _row_to_rule(rows[0]) if rows else None


async def delete_rule(conn: aiosqlite.Connection, rule_id: int) -> bool:
    cursor = await conn.execute(
        "DELETE FROM notification_rules WHERE id = ?", (rule_id,)
    )
    await conn.commit()
    invalidate_cache()
    return cursor.rowcount > 0


# ── active-rules cache ───────────────────────────────────────────────
#
# Every incoming message is matched against the active rules, so we keep
# the parsed active set in memory. The bot and the live listener share one
# event loop, so a plain module-level cache needs no locking — bot commands
# that mutate rules just call invalidate_cache().

_active_cache: list[dict] | None = None


def invalidate_cache() -> None:
    global _active_cache
    _active_cache = None


async def get_active_rules(conn: aiosqlite.Connection) -> list[dict]:
    global _active_cache
    if _active_cache is None:
        rows = await conn.execute_fetchall(
            "SELECT id, name, pattern, include_archived "
            "FROM notification_rules WHERE is_active = 1"
        )
        _active_cache = [
            {
                "id": r["id"],
                "name": r["name"],
                "compiled": compile_pattern(r["pattern"]),
                "include_archived": bool(r["include_archived"]),
            }
            for r in rows
        ]
    return _active_cache


# ── live processing & delivery ───────────────────────────────────────


def _sender_name(msg: Message) -> str:
    s = msg.sender
    if s is None or isinstance(s, (Channel, Chat)):
        return getattr(s, "title", None) or "?"
    first = getattr(s, "first_name", "") or ""
    last = getattr(s, "last_name", "") or ""
    return f"{first} {last}".strip() or "?"


def _build_annotation(matched: list[dict], chat_title: str | None, sender: str) -> str:
    names = ", ".join(f"«{r['name']}»" for r in matched)
    return (
        f"🔔 Триггер {names}\n"
        f"Чат: {chat_title or '?'}\n"
        f"От: {sender}"
    )


async def process_message(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    msg: Message,
    chat_title: str | None,
    is_archived: bool,
    target,
    bot_id: int | None = None,
    bot=None,
    owner_id: int | None = None,
) -> list[dict]:
    """Match an incoming message against active rules and alert on a hit.

    Returns the list of rules that fired (empty if none). Delivery is split:
    the **control bot** sends the text annotation to `owner_id` (a message FROM
    the bot is incoming for the owner, so Telegram pushes a real notification),
    and the **user client** forwards the original into `target` for full
    content (the bot can't see/forward the owner's chats). If the bot isn't
    available, we fall back to sending the annotation via the user client.

    Messages in the bot's own DM (`bot_id`) are skipped: that chat is where we
    deliver alerts and the control bot posts command replies, so scanning it
    would let a notification (or a /notify listing) re-trigger itself.
    """
    if msg.out:  # only incoming messages
        return []
    if target is None:
        return []
    if bot_id is not None and msg.chat_id == bot_id:
        return []

    text = msg.raw_text or ""
    if not text.strip():
        return []

    rules = await get_active_rules(conn)
    if not rules:
        return []

    matched = match_rules(text, rules, is_archived)
    if not matched:
        return []

    sender = _sender_name(msg)
    annotation = _build_annotation(matched, chat_title, sender)

    # 1. The alert text comes from the BOT, so Telegram pushes a real
    #    notification (a message from the bot is incoming for the owner).
    #    Fall back to the user client if the bot can't deliver.
    alert_sent = False
    if bot is not None and owner_id is not None:
        try:
            await bot.send_message(chat_id=owner_id, text=annotation)
            alert_sent = True
        except Exception:
            log.exception("Bot failed to send alert; falling back to user client")
    if not alert_sent:
        try:
            await client.send_message(target, annotation)
        except Exception:
            log.exception("User-client alert delivery also failed")

    # 2. The original message is forwarded by the USER CLIENT (the bot can't
    #    see or forward the owner's chats).
    try:
        await client.forward_messages(target, msg)
    except Exception:
        # Restricted-content chats can block forwarding — fall back to a
        # plain-text copy so the content is never silently lost.
        log.exception("Failed to forward message; sending text fallback")
        try:
            preview = text.replace("\n", " ")[:500]
            await client.send_message(target, f"(не удалось переслать)\n{preview}")
        except Exception:
            log.exception("Message fallback delivery also failed")

    log.info(
        "Notification: %s matched in %s",
        ", ".join(r["name"] for r in matched),
        chat_title or "?",
    )
    return matched
