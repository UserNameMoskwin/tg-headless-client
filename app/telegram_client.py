from __future__ import annotations

import logging

from telethon import TelegramClient

from app.config import settings

log = logging.getLogger(__name__)


class SessionNotAuthorized(RuntimeError):
    """The saved session is missing or revoked; run `python -m app.login`."""


def create_client() -> TelegramClient:
    return TelegramClient(
        str(settings.tg_session),
        settings.tg_api_id,
        settings.tg_api_hash,
    )


async def start_client(client: TelegramClient, interactive: bool = True) -> int:
    """Start the client and return the authorized user's id.

    interactive=True runs Telethon's login flow (asks for the code in the
    terminal). Without a terminal (systemd) pass False: we only connect with the
    saved session and raise SessionNotAuthorized instead of requesting a code —
    a restart loop would otherwise request a new code on every restart.
    """
    if interactive:
        await client.start(
            phone=settings.tg_phone,
            password=settings.tg_2fa_password,
        )
    else:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SessionNotAuthorized(f"Session {settings.tg_session} is not authorized")
    me = await client.get_me()
    log.info("Authorized as %s (id=%d)", me.first_name, me.id)
    return me.id
