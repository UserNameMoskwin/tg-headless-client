from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telethon.tl.types import Chat, MessageMediaPoll, User

from app.query import build_parser, format_row, match_chats, message_row, sender_matches


def _chat(cid: int, title: str):
    return (-cid, Chat(id=cid, title=title, photo=None, participants_count=1,
                       date=None, version=1))


def _user(first: str, username: str | None = None):
    return User(id=1, first_name=first, username=username)


CHATS = [_chat(10, "Valut.net <> Wanttopay"), _chat(11, "Didit x WTP")]


def test_match_chats_by_title_part_is_case_insensitive():
    assert match_chats(CHATS, "valut") == [CHATS[0]]
    assert match_chats(CHATS, "WTP") == [CHATS[1]]


def test_match_chats_by_id_and_no_filter():
    assert match_chats(CHATS, "-11") == [CHATS[1]]
    assert match_chats(CHATS, None) == CHATS
    assert match_chats(CHATS, "nope") == []


def test_sender_matches_name_or_username():
    natalia = _user("Natalia", "natalia_grechneva")
    assert sender_matches(natalia, "natal")
    assert sender_matches(natalia, "@Natalia_G")
    assert not sender_matches(natalia, "yulia")
    assert not sender_matches(None, "yulia")
    assert sender_matches(None, None)


def test_message_row_and_format():
    msg = SimpleNamespace(
        id=7, date=datetime(2026, 11, 18, 21, 41, tzinfo=timezone.utc),
        sender=_user("Gennady"), out=False, message="line one\nline two",
        media=MessageMediaPoll(poll=None, results=None), action=None,
    )
    row = message_row(msg, "AML", ZoneInfo("Asia/Tbilisi"))
    assert row == {
        "chat": "AML", "id": 7, "date": "2026-11-19 01:41", "sender": "Gennady",
        "out": False, "text": "line one\nline two", "media": "Poll",
    }
    assert format_row(row) == "2026-11-19 01:41 | AML | #7 | Gennady | [Poll] line one line two"


def test_parser_defaults():
    args = build_parser().parse_args(["search", "договор", "--from", "nat", "--count"])
    assert (args.cmd, args.text, args.sender, args.count, args.limit) == (
        "search", "договор", "nat", True, 20)
    args = build_parser().parse_args(["--json", "history", "--chat", "valut", "--around", "5"])
    assert (args.json, args.chat, args.around, args.limit) == (True, "valut", 5, 30)
