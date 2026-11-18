"""Read-only queries against Telegram itself (not the local DB).

Uses its own login (data/query.session, created by `python -m app.login --query`),
so it runs next to the service without touching the service's session.
Only chats of the CHAT_FOLDERS folder(s) are visible; nothing is written to
Telegram or to the DB.

    python -m app.query chats
    python -m app.query search "договор" [--chat valut] [--from natalia]
                                         [--since 2026-11-01] [--until 2026-11-18]
                                         [--limit 20] [--count]
    python -m app.query history --chat valut [--limit 30] [--since ...] [--until ...]
    python -m app.query history --chat valut --around 12345 [--limit 11]

--chat is a case-insensitive part of the title, or a chat id; without it every
folder chat is queried. Dates are local days (LOCAL_TIMEZONE), inclusive.
Add --json for one JSON object per line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

from telethon import utils
from telethon.tl import functions
from telethon.tl.types import InputDialogPeer, InputMessagesFilterEmpty

from app.config import settings
from app.folders import ChatWhitelist, _has_categories, folder_title
from app.telegram_client import create_client
from app.util import day_bounds_utc

# Messages scanned per chat when filtering client-side (--from / --since).
_SCAN_CAP = 3000


def match_chats(chats: list[tuple[int, object]], needle: str | None) -> list[tuple[int, object]]:
    """Chats whose id equals `needle` or whose title contains it (casefolded)."""
    if not needle:
        return chats
    n = needle.strip().casefold()
    return [
        (cid, e) for cid, e in chats
        if str(cid) == n or n in utils.get_display_name(e).casefold()
    ]


def sender_matches(sender, needle: str | None) -> bool:
    if not needle:
        return True
    n = needle.strip().lstrip("@").casefold()
    name = utils.get_display_name(sender).casefold() if sender is not None else ""
    username = (getattr(sender, "username", None) or "").casefold()
    return n in name or n in username


def message_row(msg, chat_title: str, tz) -> dict:
    media = type(msg.media).__name__.removeprefix("MessageMedia") if msg.media else None
    action = getattr(msg, "action", None)
    return {
        "chat": chat_title,
        "id": msg.id,
        "date": msg.date.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "sender": utils.get_display_name(msg.sender) if msg.sender is not None else None,
        "out": bool(msg.out),
        "text": msg.message or "",
        "media": media or (type(action).__name__ if action else None),
    }


def format_row(row: dict) -> str:
    text = " ".join(row["text"].split())
    if row["media"]:
        text = f"[{row['media']}] {text}".strip()
    return f"{row['date']} | {row['chat']} | #{row['id']} | {row['sender'] or '—'} | {text}"


def _bounds(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    """UTC [start, end) for the inclusive local-day range."""
    start = datetime.fromisoformat(day_bounds_utc(since, settings.tz)[0]) if since else None
    end = datetime.fromisoformat(day_bounds_utc(until, settings.tz)[1]) if until else None
    return start, end


async def folder_chats(client) -> list[tuple[int, object]]:
    """(chat_id, entity) for every chat the folder whitelist allows."""
    names = settings.chat_folder_names
    if names:
        result = await client(functions.messages.GetDialogFiltersRequest())
        folders = getattr(result, "filters", result)
        wanted = {n.casefold() for n in names}
        matched = [f for f in folders if folder_title(f).casefold() in wanted
                   and hasattr(f, "include_peers")]
        if matched and not any(_has_categories(f) for f in matched):
            # Explicit peers only: one request instead of paging every dialog.
            peers = [p for f in matched for p in list(f.pinned_peers) + list(f.include_peers)]
            res = await client(functions.messages.GetPeerDialogsRequest(
                peers=[InputDialogPeer(p) for p in peers]))
            ents = {utils.get_peer_id(e): e for e in list(res.users) + list(res.chats)}
            ids = [utils.get_peer_id(d.peer) for d in res.dialogs]
            return [(i, ents[i]) for i in dict.fromkeys(ids) if i in ents]
    whitelist = ChatWhitelist(names)
    await whitelist.refresh(client)
    return [(d.id, d.entity) async for d in client.iter_dialogs() if whitelist.allows(d.id)]


async def _iter_rows(client, entity, title, args, search: str | None):
    start, end = _bounds(args.since, args.until)
    found = scanned = 0
    async for msg in client.iter_messages(entity, search=search, offset_date=end):
        scanned += 1
        if (start and msg.date < start) or scanned > _SCAN_CAP:
            break
        if not sender_matches(msg.sender, args.sender):
            continue
        yield message_row(msg, title, settings.tz)
        found += 1
        if found >= args.limit:
            break


async def _count(client, entity, args) -> int:
    start, end = _bounds(args.since, args.until)
    res = await client(functions.messages.SearchRequest(
        peer=entity, q=args.text, filter=InputMessagesFilterEmpty(),
        min_date=start, max_date=end, offset_id=0, add_offset=0, limit=1,
        max_id=0, min_id=0, hash=0))
    return getattr(res, "count", len(res.messages))


async def _run(args) -> int:
    emit = (lambda r: print(json.dumps(r, ensure_ascii=False))) if args.json else \
           (lambda r: print(format_row(r) if "id" in r else " | ".join(str(v) for v in r.values())))
    client = create_client(settings.tg_query_session, receive_updates=False)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("Query session is not authorized. Run: python -m app.login --query",
                  file=sys.stderr)
            return 1
        chats = await folder_chats(client)
        if args.cmd == "chats":
            for cid, e in chats:
                emit({"chat_id": cid, "kind": type(e).__name__, "title": utils.get_display_name(e)})
            return 0

        chats = match_chats(chats, args.chat)
        if not chats:
            print(f"No folder chat matches {args.chat!r}", file=sys.stderr)
            return 1

        if args.cmd == "search" and args.count:
            for cid, e in chats:
                n = await _count(client, e, args)
                if n:
                    emit({"chat": utils.get_display_name(e), "count": n})
            return 0

        if args.cmd == "history" and args.around:
            if len(chats) != 1:
                print("--around needs --chat matching exactly one chat", file=sys.stderr)
                return 1
            _, e = chats[0]
            half = args.limit // 2
            msgs = await client.get_messages(
                e, limit=2 * half + 1, offset_id=args.around, add_offset=-(half + 1))
            for msg in reversed(msgs):
                emit(message_row(msg, utils.get_display_name(e), settings.tz))
            return 0

        if args.cmd == "history" and len(chats) != 1:
            print("history needs --chat matching exactly one chat; matches: "
                  + ", ".join(utils.get_display_name(e) for _, e in chats), file=sys.stderr)
            return 1

        rows = []
        for cid, e in chats:
            title = utils.get_display_name(e)
            async for row in _iter_rows(client, e, title, args,
                                        args.text if args.cmd == "search" else None):
                rows.append(row)
        rows.sort(key=lambda r: (r["date"], r["id"]), reverse=args.cmd == "search")
        for row in rows:
            emit(row)
        return 0
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.query", description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="one JSON object per line")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chats", help="list the folder chats")

    def common(p, default_limit):
        p.add_argument("--chat", help="part of the chat title, or a chat id")
        p.add_argument("--from", dest="sender", help="part of the sender name or username")
        p.add_argument("--since", help="first local day, YYYY-MM-DD")
        p.add_argument("--until", help="last local day, YYYY-MM-DD")
        p.add_argument("--limit", type=int, default=default_limit, help="messages per chat")

    s = sub.add_parser("search", help="Telegram-side search in the folder chats")
    s.add_argument("text")
    s.add_argument("--count", action="store_true",
                   help="only the number of matches per chat (ignores --from)")
    common(s, 20)
    h = sub.add_parser("history", help="recent messages of one chat")
    h.add_argument("--around", type=int, help="message id to show context for")
    common(h, 30)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
