from __future__ import annotations

import logging

from telegram import Update

from app.config import settings

log = logging.getLogger(__name__)


def is_allowed(update: Update) -> bool:
    if not update.effective_chat or update.effective_chat.type != "private":
        log.debug("Ignored non-private chat message")
        return False

    user = update.effective_user
    if not user:
        log.warning("Message with no effective_user")
        return False

    if user.id != settings.allowed_telegram_user_id:
        log.warning("Denied access for user_id=%d (%s)", user.id, user.full_name)
        return False

    return True
