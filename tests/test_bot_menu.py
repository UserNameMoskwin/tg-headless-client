from __future__ import annotations

from app.bot import handlers


def test_help_text_lists_every_command():
    text = handlers._help_text()
    for cmd, desc in handlers.COMMANDS:
        assert f"/{cmd}" in text
        assert desc in text


def test_button_labels_unique():
    assert len(set(handlers.BUTTON_LABELS)) == len(handlers.BUTTON_LABELS)


def test_bot_commands_are_valid():
    cmds = handlers.bot_commands()
    assert cmds
    for c in cmds:
        # Telegram constraints: lowercase command, 1..256 char description
        assert c.command == c.command.lower()
        assert 1 <= len(c.description) <= 256


def test_keyboard_buttons_match_labels():
    flat = [btn.text for row in handlers.MAIN_KEYBOARD.keyboard for btn in row]
    assert sorted(flat) == sorted(handlers.BUTTON_LABELS)
