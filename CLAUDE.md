# CLAUDE.md

Guidance for Claude Code working in this repo. Keep edits minimal and match the surrounding style. For a fuller tour see the "LLM agent guide" section at the top of [README.md](README.md).

## What this is

Personal, local Telegram ingestion server (Windows-only). A Telethon **user client** reads every message into SQLite + daily JSONL; a separate **control bot** (python-telegram-bot) exposes commands. One process, one asyncio loop hosting both clients. No HTTP API — by design. Keep it simple; the owner explicitly declined an HTTP layer and over-engineering.

Stack: Python 3.12+ (runs on 3.14), Telethon (MTProto), python-telegram-bot 22.x, aiosqlite (WAL), pydantic-settings, snowballstemmer.

## Commands

```bash
pip install -e ".[dev]"          # install (incl. pytest, pytest-asyncio)
python -m app.main               # run the server (first run: enter Telethon login code in terminal)
pytest -q                        # full suite (~43 tests)
pytest tests/test_notifications.py -q          # one file
pytest tests/test_stats.py::TestDailyStats -q  # one class
pytest -k morphology -q          # by keyword

python -m app.backfill_from_export [result.json]   # import Telegram Desktop export
python -m app.style_analyzer                        # build writing-style profile
```

Notes:
- The shell is PowerShell; the Bash tool is also available. When piping Cyrillic into `python`, the PowerShell pipe corrupts UTF-8 — set `$env:PYTHONIOENCODING="utf-8"` and prefer running a file over `python -c` for non-ASCII.
- `python -m app.main` needs real Telegram credentials in `.env` and network; it can't be run headlessly here. Rely on `pytest` for verification.

## Layout

```
app/
  main.py            — Entry point (_run): startup, terminal UI, on_new_message live handler,
                       notification dispatch, archived-chat cache, setup_commands() call, shutdown.
  config.py          — pydantic-settings from .env via a LAZY proxy `settings`.
  db.py              — SQLite schema (4 tables) + init_db()/get_connection(), WAL.
  telegram_client.py — Telethon create/start (2FA via tg_2fa_password).
  ingest.py          — save_message() → INSERT OR IGNORE into messages.
  backfill.py        — run_backfill(): concurrent dialog scan, day-level tracking, participation filter.
  daily_log.py       — JSONL daily files; append_message(), rebuild_day_from_db().
  export.py          — export_my_messages() → data/exports/*.jsonl.
  backfill_from_export.py / style_analyzer.py — standalone CLIs.
  bot/
    bot.py           — create_bot_app(): registers handlers; setup_commands() (native "Menu" list).
    handlers.py      — all commands, MAIN_KEYBOARD, COMMANDS, /help, on_button router,
                       /notify* (incl. the add/edit ConversationHandler steps).
    guards.py        — is_allowed(): private chat + user_id whitelist.
  services/          — conn-in, dicts-out logic (unit-tested): stats, messages, jobs, notifications.
tests/               — pytest + pytest-asyncio.
data/  (gitignored)  — telegram.sqlite3, telegram.session, days/*.jsonl, exports/*.jsonl.
```

## Conventions

- **Async everywhere**; both clients share one asyncio loop in `main._run()`.
- **`settings` is a lazy proxy** (config.py) — importing modules never reads `.env`, which keeps tests import-safe. Always `from app.config import settings`.
- **SQLite**: aiosqlite, WAL, `row_factory = Row`, writes use `INSERT OR IGNORE` for idempotency.
- **Services hold the logic** (`app/services/*`, take a `conn`, return plain dicts) and are where unit tests live. **Bot handlers stay thin** and delegate.
- **Time**: store UTC; convert to `settings.tz` (default `Asia/Tbilisi`) only at day boundaries.
- Bilingual surface: user-facing bot text is Russian; code/comments are English.

## Two identities (matters for any "send/forward" work)

- **Telethon user client** = the owner's account. Sees all dialogs, can truly *forward* messages. Drives ingestion and the **forward** half of notification delivery.
- **Control bot** (Bot API) = command surface; it cannot read the owner's chats or forward from them, but it **can DM the owner**. Notification delivery is split: the **bot** sends the alert text to `owner_id` (a message *from* the bot is incoming for the owner, so Telegram fires a real push), and the **user client** forwards the original into the bot DM (`target`) for full content. If the bot can't deliver, the user client sends the alert text too (no push, but never lost). All four — `target`, `bot_id`, `bot`, `owner_id` — are resolved in `main._run()`.

## Gotchas (read before changing related code)

- **PTB lifecycle is manual** (`async with app: start(); updater.start_polling()`), so PTB's `post_init` is **never called**. Startup-only bot setup must be invoked explicitly in `main._run()` — that's how the native command menu is set (`bot.setup_commands()`). Don't add a `post_init` and expect it to run.
- **`db.py` schema is mirrored inline in `tests/conftest.py`.** When you add/alter a table or column, update **both** or the tests drift from production.
- **Notifications run *before* the archived-skip** in `on_new_message`, so keyword alerts cover every chat regardless of archive/Telegram-mute state; ingestion may still skip archived chats when `SKIP_ARCHIVED=true`. Keep that ordering.
- **Notifications skip the bot's own DM.** Alerts and the control bot's command replies (e.g. a `/notify` listing) land in the owner↔bot DM; that chat's `chat_id` equals the bot's user id. `process_message(..., bot_id=...)` drops messages from it so a delivered alert (or a rule listing) can't re-trigger itself. `bot_id` is resolved next to `notify_target` in `main._run()`.
- **Keyword matching is morphological, not substring**: keywords and text are stemmed per word (snowballstemmer RU/EN, `ё`→`е` normalized) and compared as token stems — `сделать` fires on `сделаем/сделаю/…`, but `кот` does *not* match `который`. Active rules are cached in `notifications._active_cache`; **every rule mutation must call `invalidate_cache()`**.
- **Reply keyboard ≠ command menu.** The persistent on-screen keyboard is a `ReplyKeyboardMarkup` attached per message (`handlers.MAIN_KEYBOARD`); the "Menu" button list is `setMyCommands` (`handlers.COMMANDS`). Both are wired in `bot/bot.py`. Adding a command usually means: handler in `handlers.py` → register in `bot.py` → add to `COMMANDS` (and a button only if it takes no args).
- **`/notify_add` and `/notify_edit` are a `ConversationHandler`** (registered in `bot.py`), not plain command handlers. The flow is name → keywords → archived (inline) → active (inline), state in `context.user_data["notify_draft"]`; both share the same steps and the `edit_id` field selects add vs. edit (edit pre-loads the rule and lets `-` keep a value). Step functions return the next `NOTIFY_*` state; the inline buttons use `callback_data` prefixes `notify_arch:`/`notify_active:` that the `CallbackQueryHandler` patterns must keep matching. `/cancel` is the fallback. The edit save path calls `notifications.update_rule()` (overwrites all fields at once); the add path uses `add_rule` + `set_active`/`set_archived`.

## Adding things — quick recipes

- **New bot command**: add `cmd_x` in `handlers.py` (guard with `is_allowed`), register in `bot.py`, add to `COMMANDS`. If no-arg and "main", add a button to `MAIN_KEYBOARD` + `BUTTON_LABELS` and a branch in `on_button`.
- **New DB table/column**: edit `db._SCHEMA`, mirror it in `tests/conftest.py`, then add a service function + test.
- **New service**: put pure-ish, conn-in/dicts-out logic in `app/services/`, unit-test it; call it from a thin handler.

## House style

Match `README.md`'s existing markdown conventions (headings sit directly on their text; `**…:**` blocks are followed directly by lists). Don't reflow untouched sections to satisfy a linter.
