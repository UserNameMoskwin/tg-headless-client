from __future__ import annotations

import pytest

from app.services import notifications
from app.services.notifications import (
    compile_pattern,
    match_rules,
    parse_pattern,
    pattern_matches,
    text_to_stems,
)


class TestParsePattern:
    def test_single_keyword(self):
        assert parse_pattern("дедлайн") == [["дедлайн"]]

    def test_or_alternatives(self):
        assert parse_pattern("кот, котик, мур") == [["кот"], ["котик"], ["мур"]]

    def test_and_combination(self):
        assert parse_pattern("отчёт + срочно") == [["отчёт", "срочно"]]

    def test_mixed_and_or(self):
        assert parse_pattern("дедлайн, отчёт + срочно") == [
            ["дедлайн"],
            ["отчёт", "срочно"],
        ]

    def test_lowercases_and_trims(self):
        assert parse_pattern("  ОтЧёт  +  Срочно  ") == [["отчёт", "срочно"]]

    def test_drops_empty_terms(self):
        assert parse_pattern("кот, , +, +мур+") == [["кот"], ["мур"]]


class TestMorphologyMatching:
    def _match(self, pattern: str, text: str) -> bool:
        return pattern_matches(compile_pattern(pattern), text_to_stems(text))

    def test_inflected_verb_forms(self):
        # the user's example: keyword "сделать" must fire on its conjugations
        for form in ["сделай это", "сделайте отчёт", "мы сделаем",
                     "я сделаю", "уже сделал", "сделали вчера"]:
            assert self._match("сделать", form), form

    def test_noun_declensions(self):
        for form in ["новые отчёты", "жду отчёта", "с отчётом", "в отчётах"]:
            assert self._match("отчёт", form), form

    def test_yo_normalization(self):
        # ё/е are unified, so the keyword matches regardless of which is used
        assert self._match("отчет", "готовь отчёт")
        assert self._match("отчёт", "готовь отчет")

    def test_english_inflections(self):
        for form in ["the meeting", "two meetings", "we meet"]:
            assert self._match("meeting", form), form

    def test_no_false_substring_merge(self):
        # "кот" must NOT fire on unrelated words that merely contain it
        assert not self._match("кот", "который час")
        assert not self._match("кот", "выпил кофе")
        # but real declensions still match
        assert self._match("кот", "у меня кот")
        assert self._match("кот", "вижу кота")

    def test_and_requires_all_terms(self):
        assert self._match("отчёт + срочно", "срочно нужен отчёт")
        assert not self._match("отчёт + срочно", "нужен отчёт к пятнице")

    def test_or_any_alternative(self):
        pattern = "дедлайн, отчёт + срочно"
        assert self._match(pattern, "горят дедлайны")
        assert self._match(pattern, "срочно сдай отчёты")
        assert not self._match(pattern, "обычное сообщение")


class TestMatchRules:
    def _rules(self):
        return [
            {"id": 1, "name": "A", "compiled": compile_pattern("кот"), "include_archived": False},
            {"id": 2, "name": "B", "compiled": compile_pattern("пёс"), "include_archived": True},
        ]

    def test_returns_all_matching(self):
        matched = match_rules("кот и пёс", self._rules(), is_archived=False)
        assert {r["name"] for r in matched} == {"A", "B"}

    def test_archived_chat_skips_non_archived_rules(self):
        matched = match_rules("кот и пёс", self._rules(), is_archived=True)
        # only rule B opts into archived chats
        assert {r["name"] for r in matched} == {"B"}

    def test_no_match(self):
        assert match_rules("ничего", self._rules(), is_archived=False) == []


@pytest.mark.asyncio
class TestRuleCrud:
    async def test_add_and_list(self, db_conn):
        rid = await notifications.add_rule(db_conn, "Работа", "дедлайн, отчёт + срочно")
        rules = await notifications.list_rules(db_conn)
        assert len(rules) == 1
        assert rules[0]["id"] == rid
        assert rules[0]["name"] == "Работа"
        assert rules[0]["is_active"] is True
        assert rules[0]["include_archived"] is False

    async def test_toggle_active_and_archived(self, db_conn):
        rid = await notifications.add_rule(db_conn, "X", "кот")
        assert await notifications.set_active(db_conn, rid, False)
        assert await notifications.set_archived(db_conn, rid, True)
        rule = await notifications.get_rule(db_conn, rid)
        assert rule["is_active"] is False
        assert rule["include_archived"] is True

    async def test_update_rule_overwrites_all_fields(self, db_conn):
        rid = await notifications.add_rule(db_conn, "Старое", "кот")
        ok = await notifications.update_rule(
            db_conn,
            rid,
            name="Новое",
            pattern="пёс, отчёт + срочно",
            is_active=False,
            include_archived=True,
        )
        assert ok
        rule = await notifications.get_rule(db_conn, rid)
        assert rule["name"] == "Новое"
        assert rule["pattern"] == "пёс, отчёт + срочно"
        assert rule["is_active"] is False
        assert rule["include_archived"] is True

    async def test_update_missing_rule_returns_false(self, db_conn):
        assert not await notifications.update_rule(
            db_conn, 999, name="x", pattern="y", is_active=True, include_archived=False
        )

    async def test_delete(self, db_conn):
        rid = await notifications.add_rule(db_conn, "X", "кот")
        assert await notifications.delete_rule(db_conn, rid)
        assert await notifications.get_rule(db_conn, rid) is None

    async def test_missing_rule_returns_false(self, db_conn):
        assert not await notifications.set_active(db_conn, 999, True)
        assert not await notifications.delete_rule(db_conn, 999)

    async def test_active_cache_only_includes_active(self, db_conn):
        notifications.invalidate_cache()
        active_id = await notifications.add_rule(db_conn, "On", "кот")
        off_id = await notifications.add_rule(db_conn, "Off", "пёс")
        await notifications.set_active(db_conn, off_id, False)

        active = await notifications.get_active_rules(db_conn)
        assert [r["id"] for r in active] == [active_id]
        # compiled (stemmed) form is cached, ready for matching
        assert active[0]["compiled"] == [[["кот"]]]


class _FakeMessage:
    def __init__(self, *, text: str, chat_id: int, out: bool = False):
        self.raw_text = text
        self.chat_id = chat_id
        self.out = out
        self.sender = None


class _FakeClient:
    def __init__(self):
        self.sent: list = []
        self.forwarded: list = []

    async def send_message(self, target, text):
        self.sent.append((target, text))

    async def forward_messages(self, target, msg):
        self.forwarded.append((target, msg))


@pytest.mark.asyncio
class TestProcessMessage:
    async def _rule(self, db_conn):
        notifications.invalidate_cache()
        return await notifications.add_rule(db_conn, "Работа", "operations")

    async def test_forwards_on_match(self, db_conn):
        await self._rule(db_conn)
        client = _FakeClient()
        msg = _FakeMessage(text="head of operations", chat_id=555)
        matched = await notifications.process_message(
            client, db_conn, msg, "Some Chat", False, target="dm", bot_id=999
        )
        assert [r["name"] for r in matched] == ["Работа"]
        assert client.forwarded  # original was forwarded

    async def test_skips_bot_own_dm(self, db_conn):
        # the alert/command-reply chat must never re-trigger itself
        await self._rule(db_conn)
        client = _FakeClient()
        msg = _FakeMessage(text="head of operations", chat_id=999)
        matched = await notifications.process_message(
            client, db_conn, msg, "Bot DM", False, target="dm", bot_id=999
        )
        assert matched == []
        assert not client.sent and not client.forwarded
