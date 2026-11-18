from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import telegram_client
from app.telegram_client import SessionNotAuthorized, start_client


class _FakeClient:
    def __init__(self, authorized: bool):
        self.authorized = authorized
        self.started = False
        self.disconnected = False

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def disconnect(self):
        self.disconnected = True

    async def start(self, **kwargs):
        self.started = True

    async def get_me(self):
        return SimpleNamespace(id=42, first_name="Me")


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        telegram_client, "settings",
        SimpleNamespace(tg_phone="+1", tg_2fa_password=None, tg_session="s.session"),
    )


async def test_non_interactive_never_requests_a_code():
    client = _FakeClient(authorized=False)
    with pytest.raises(SessionNotAuthorized):
        await start_client(client, interactive=False)
    assert not client.started
    assert client.disconnected


async def test_non_interactive_with_valid_session():
    client = _FakeClient(authorized=True)
    assert await start_client(client, interactive=False) == 42
    assert not client.started


async def test_interactive_uses_login_flow():
    client = _FakeClient(authorized=False)
    assert await start_client(client, interactive=True) == 42
    assert client.started
