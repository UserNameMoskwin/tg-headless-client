from __future__ import annotations

import logging

from telethon import TelegramClient

from app.config import settings

log = logging.getLogger(__name__)


def create_client() -> TelegramClient:
    return TelegramClient(
        str(settings.tg_session),
        settings.tg_api_id,
        settings.tg_api_hash,
    )


async def start_client(client: TelegramClient) -> int:
    """Start the client and return the authorized user's id."""
    await client.start(
        phone=settings.tg_phone,
        password=settings.tg_2fa_password,
    )
    me = await client.get_me()
    log.info("Authorized as %s (id=%d)", me.first_name, me.id)
    return me.id
