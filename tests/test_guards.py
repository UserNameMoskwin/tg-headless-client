from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.bot.guards import is_allowed


def _make_update(*, chat_type="private", user_id=123456789):
    update = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_user.id = user_id
    update.effective_user.full_name = "Test User"
    return update


class TestGuards:
    def test_allowed_user(self, monkeypatch):
        monkeypatch.setattr("app.bot.guards.settings", SimpleNamespace(allowed_telegram_user_id=123456789))
        assert is_allowed(_make_update(user_id=123456789)) is True

    def test_denied_user(self, monkeypatch):
        monkeypatch.setattr("app.bot.guards.settings", SimpleNamespace(allowed_telegram_user_id=123456789))
        assert is_allowed(_make_update(user_id=999)) is False

    def test_non_private_chat(self, monkeypatch):
        monkeypatch.setattr("app.bot.guards.settings", SimpleNamespace(allowed_telegram_user_id=123456789))
        assert is_allowed(_make_update(chat_type="group", user_id=123456789)) is False
