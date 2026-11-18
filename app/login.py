"""Interactive Telegram login: creates/refreshes the session file.

Run in a terminal (on the server: over SSH, as the service user):
    python -m app.login

Telegram sends the code to your Telegram app; type it here. The 2FA password
is taken from TG_2FA_PASSWORD or asked for. Never paste the code into a
Telegram chat: Telegram expires login codes that are sent in messages.
"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.telegram_client import create_client, start_client


async def _main() -> None:
    settings.tg_session.parent.mkdir(parents=True, exist_ok=True)
    client = create_client()
    try:
        me_id = await start_client(client, interactive=True)
        me = await client.get_me()
        print(f"Authorized as {me.first_name} (id={me_id}). Session: {settings.tg_session}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
