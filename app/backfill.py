"""One-off backfill: copy recent history of the folder chats into the DB.

Run by hand; the service never calls it. It reads Telegram through the query
session (data/query.session, see app.query), so the service keeps running, and
writes the same rows as live ingestion (INSERT OR IGNORE: safe to repeat).
No notifications are fired for backfilled messages.

    python -m app.backfill --days 30 [--chat valut]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from telethon import utils

from app.config import settings
from app.db import get_connection, init_db
from app.ingest import save_message
from app.query import folder_chats, match_chats
from app.telegram_client import create_client

_COMMIT_EVERY = 200


async def backfill_chat(conn, client, entity, title: str | None, cutoff: datetime) -> tuple[int, int]:
    """Save messages of one chat newer than `cutoff`. Returns (seen, inserted)."""
    seen = inserted = 0
    async for msg in client.iter_messages(entity):
        if msg.date and msg.date < cutoff:
            break
        seen += 1
        inserted += await save_message(conn, msg, chat_title=title)
        if seen % _COMMIT_EVERY == 0:
            await conn.commit()
    await conn.commit()
    return seen, inserted


async def _run(args) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    client = create_client(settings.tg_query_session, receive_updates=False)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("Query session is not authorized. Run: python -m app.login --query",
                  file=sys.stderr)
            return 1
        chats = match_chats(await folder_chats(client), args.chat)
        if not chats:
            print(f"No folder chat matches {args.chat!r}", file=sys.stderr)
            return 1
        await init_db()
        conn = await get_connection()
        try:
            # the service writes to the same DB: wait for its short transactions
            await conn.execute("PRAGMA busy_timeout=30000")
            total_seen = total_new = 0
            for _, entity in chats:
                # same title rule as the live handler
                title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
                seen, new = await backfill_chat(conn, client, entity, title, cutoff)
                total_seen += seen
                total_new += new
                print(f"{utils.get_display_name(entity)} | seen {seen} | new {new}", flush=True)
            print(f"Done: {len(chats)} chats, seen {total_seen}, new {total_new}")
        finally:
            await conn.close()
        return 0
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backfill",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, required=True, help="how many days back to load")
    parser.add_argument("--chat", help="part of the chat title, or a chat id (default: all)")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
