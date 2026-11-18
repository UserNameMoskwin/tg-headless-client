from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon import utils
from telethon.tl.types import (
    Channel,
    Chat,
    ChatPhotoEmpty,
    DialogFilter,
    DialogFilterDefault,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    TextWithEntities,
    User,
)
from telethon.tl.types.messages import DialogFilters

from app.folders import ChatWhitelist, folder_chat_ids, folder_title


def _title(text: str) -> TextWithEntities:
    return TextWithEntities(text=text, entities=[])


def _folder(title="WTP", pinned=(), include=(), exclude=(), **flags) -> DialogFilter:
    return DialogFilter(
        id=2, title=_title(title),
        pinned_peers=list(pinned), include_peers=list(include), exclude_peers=list(exclude),
        **flags,
    )


def _megagroup(cid: int) -> Channel:
    return Channel(id=cid, title="g", photo=ChatPhotoEmpty(), date=None, megagroup=True)


def _broadcast(cid: int) -> Channel:
    return Channel(id=cid, title="c", photo=ChatPhotoEmpty(), date=None, broadcast=True)


def _dialog(entity, archived=False):
    return SimpleNamespace(id=utils.get_peer_id(entity), entity=entity, archived=archived)


def _ch(cid: int) -> int:
    """Marked id of a channel, as in msg.chat_id."""
    return utils.get_peer_id(InputPeerChannel(channel_id=cid, access_hash=1))


class TestFolderChatIds:
    def test_explicit_peers_are_marked_ids(self):
        f = _folder(
            pinned=[InputPeerChannel(channel_id=111, access_hash=1)],
            include=[InputPeerChat(chat_id=222), InputPeerUser(user_id=333, access_hash=1)],
        )
        assert folder_chat_ids(f) == {_ch(111), -222, 333}

    def test_title_from_text_with_entities(self):
        assert folder_title(_folder(title=" WTP ")) == "WTP"

    def test_groups_category_with_exclusion(self):
        f = _folder(groups=True, exclude=[InputPeerChannel(channel_id=2, access_hash=1)])
        dialogs = [
            _dialog(_megagroup(1)),
            _dialog(_megagroup(2)),          # excluded
            _dialog(_broadcast(3)),          # channel, not a group
            _dialog(Chat(id=55, title="old", photo=ChatPhotoEmpty(),
                         participants_count=2, date=None, version=1)),
            _dialog(User(id=7, contact=True)),
        ]
        assert folder_chat_ids(f, dialogs) == {_ch(1), -55}

    def test_exclude_archived(self):
        f = _folder(broadcasts=True, exclude_archived=True)
        dialogs = [_dialog(_broadcast(3)), _dialog(_broadcast(4), archived=True)]
        assert folder_chat_ids(f, dialogs) == {_ch(3)}

    def test_user_categories(self):
        f = _folder(contacts=True, bots=True)
        dialogs = [
            _dialog(User(id=1, contact=True)),
            _dialog(User(id=2, contact=False)),
            _dialog(User(id=3, bot=True)),
        ]
        assert folder_chat_ids(f, dialogs) == {1, 3}


class _FakeClient:
    def __init__(self, folders, dialogs=()):
        self._folders = folders
        self._dialogs = list(dialogs)
        self.iterated = False

    async def __call__(self, request):
        return DialogFilters(filters=self._folders)

    async def iter_dialogs(self):
        self.iterated = True
        for d in self._dialogs:
            yield d


class TestChatWhitelist:
    def test_disabled_allows_everything(self):
        wl = ChatWhitelist([])
        assert not wl.enabled
        assert wl.allows(123)

    async def test_refresh_matches_folder_case_insensitive(self):
        folders = [
            DialogFilterDefault(),
            _folder(title="wtp", include=[InputPeerChannel(channel_id=111, access_hash=1)]),
            _folder(title="Personal", include=[InputPeerUser(user_id=9, access_hash=1)]),
        ]
        client = _FakeClient(folders)
        wl = ChatWhitelist(["WTP"])
        await wl.refresh(client)
        assert wl.allows(_ch(111))
        assert not wl.allows(9)
        assert wl.found_folders == ["wtp"]
        assert not client.iterated  # no category flags → no dialog scan

    async def test_missing_folder_fails_closed(self):
        wl = ChatWhitelist(["WTP"])
        await wl.refresh(_FakeClient([_folder(title="Other", include=[InputPeerChat(chat_id=1)])]))
        assert wl.enabled
        assert wl.chat_ids == set()
        assert not wl.allows(-1)

    async def test_refresh_error_keeps_previous_set(self):
        wl = ChatWhitelist(["WTP"])
        await wl.refresh(_FakeClient([_folder(include=[InputPeerChat(chat_id=1)])]))

        class _Broken:
            async def __call__(self, request):
                raise ConnectionError

        with pytest.raises(ConnectionError):
            await wl.refresh(_Broken())
        assert wl.allows(-1)

    async def test_category_folder_scans_dialogs(self):
        client = _FakeClient([_folder(groups=True)], [_dialog(_megagroup(1))])
        wl = ChatWhitelist(["WTP"])
        await wl.refresh(client)
        assert client.iterated
        assert wl.allows(_ch(1))
