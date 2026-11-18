"""Chat whitelist by Telegram folder (dialog filter).

A folder is defined by explicit peers (pinned + included) and/or category
flags (contacts, non-contacts, groups, channels, bots) minus excluded peers.
Dynamic conditions (exclude_muted, exclude_read) are ignored: membership must
not flip every time a chat is read or muted.

The whitelist fails closed: if a configured folder can't be found or loaded,
no chats are processed until the next successful refresh.
"""
from __future__ import annotations

import logging
from typing import Iterable

from telethon import utils
from telethon.tl import functions
from telethon.tl.types import Channel, Chat, DialogFilter, DialogFilterChatlist, User

log = logging.getLogger(__name__)

_CATEGORY_FLAGS = ("contacts", "non_contacts", "groups", "broadcasts", "bots")


def folder_title(folder) -> str:
    """Folder title as plain text (newer layers wrap it in TextWithEntities)."""
    title = getattr(folder, "title", "") or ""
    return (getattr(title, "text", title) or "").strip()


def _peer_ids(peers) -> set[int]:
    ids: set[int] = set()
    for peer in peers or []:
        try:
            ids.add(utils.get_peer_id(peer))
        except Exception:
            log.debug("Skipping unsupported folder peer %r", peer)
    return ids


def _has_categories(folder) -> bool:
    return isinstance(folder, DialogFilter) and any(
        getattr(folder, flag, False) for flag in _CATEGORY_FLAGS
    )


def _in_category(folder, entity) -> bool:
    if isinstance(entity, User):
        if entity.bot:
            return bool(folder.bots)
        return bool(folder.contacts) if entity.contact else bool(folder.non_contacts)
    if isinstance(entity, Chat):
        return bool(folder.groups)
    if isinstance(entity, Channel):
        return bool(folder.broadcasts) if entity.broadcast else bool(folder.groups)
    return False


def folder_chat_ids(folder, dialogs: Iterable = ()) -> set[int]:
    """Chat ids (marked, as in msg.chat_id) that belong to a folder.

    `dialogs` (objects with .id, .entity, .archived) is only consulted when
    the folder uses category flags.
    """
    ids = _peer_ids(folder.pinned_peers) | _peer_ids(folder.include_peers)
    if _has_categories(folder):
        excluded = _peer_ids(folder.exclude_peers)
        for d in dialogs:
            if folder.exclude_archived and d.archived:
                continue
            if d.id not in excluded and _in_category(folder, d.entity):
                ids.add(d.id)
    return ids


class ChatWhitelist:
    """Chats the server processes. No folder names configured = every chat."""

    def __init__(self, folder_names: Iterable[str]):
        self.folder_names = [n.strip() for n in folder_names if n and n.strip()]
        self.chat_ids: set[int] = set()
        self.found_folders: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.folder_names)

    def allows(self, chat_id: int | None) -> bool:
        return not self.enabled or chat_id in self.chat_ids

    def describe(self) -> str:
        if not self.enabled:
            return "все чаты"
        names = ", ".join(self.folder_names)
        return f"папка {names} — {len(self.chat_ids)} чат(ов)"

    async def refresh(self, client) -> None:
        """Reload folder membership from Telegram. Keeps the old set on error."""
        if not self.enabled:
            return
        result = await client(functions.messages.GetDialogFiltersRequest())
        folders = getattr(result, "filters", result)
        wanted = {n.casefold() for n in self.folder_names}
        matched = [
            f for f in folders
            if isinstance(f, (DialogFilter, DialogFilterChatlist))
            and folder_title(f).casefold() in wanted
        ]

        dialogs: list = []
        if any(_has_categories(f) for f in matched):
            dialogs = [d async for d in client.iter_dialogs()]

        ids: set[int] = set()
        for f in matched:
            ids |= folder_chat_ids(f, dialogs)

        found = [folder_title(f) for f in matched]
        missing = wanted - {t.casefold() for t in found}
        if missing:
            log.warning("Telegram folder(s) not found: %s", ", ".join(sorted(missing)))
        if ids != self.chat_ids:
            log.info("Chat whitelist: %d chats in %s", len(ids), ", ".join(found) or "—")
        self.chat_ids = ids
        self.found_folders = found
