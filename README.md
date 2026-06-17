# headless-tgclient

Personal headless Telegram client for Windows. Authorizes as a user via MTProto, indexes messages into SQLite, provides control via Telegram bot. Runs locally, no public API.

---

## 🤖 LLM agent guide (read this first)

**What this is:** a personal, local Telegram ingestion server (Windows). A Telethon *user client* reads every message into SQLite + daily JSONL; a separate *control bot* (python-telegram-bot) exposes commands. No HTTP API — by design.

**Stack:** Python 3.12+ (runs on 3.14), Telethon (MTProto), python-telegram-bot 22.x, aiosqlite (WAL), pydantic-settings, snowballstemmer. One process, one asyncio loop hosting both clients.

**Start reading at** [`app/main.py`](app/main.py) → `_run()`: logging/terminal UI, Telethon start, archived-chat cache, the `on_new_message` live handler, then the control-bot lifecycle.

### File map

```
app/
  main.py            — Entry point (_run). Startup, ANSI terminal UI, live handler (on_new_message),
                       notification dispatch + forward target, archived-chat cache, setup_commands() call.
  config.py          — Pydantic Settings from .env via a LAZY proxy (settings). Fields: tg_*,
                       control_bot_token, allowed_telegram_user_ids, skip_archived, local_timezone.
  db.py              — SQLite schema (4 tables) + init_db()/get_connection(). WAL mode.
  telegram_client.py — Telethon client create/start. 2FA via tg_2fa_password.
  ingest.py          — save_message() — INSERT OR IGNORE into messages. Returns inserted/duplicate.
  backfill.py        — run_backfill() — concurrent dialog scan (Semaphore 5), day-level tracking,
                       user-participation filter cached in ingest_state.
  daily_log.py       — JSONL daily files in data/days/. append_message(), rebuild_day_from_db().
  export.py          — export_my_messages() — SQL query → data/exports/*.jsonl.
  backfill_from_export.py — Import full history from Telegram Desktop result.json (CLI).
  style_analyzer.py  — Writing-style profile from exported messages → style_profile.md/.json (CLI).

  bot/
    bot.py           — create_bot_app(): registers command + button handlers, stores tg_client/db_conn
                       in bot_data. setup_commands() sets the native "Menu" command list.
    handlers.py      — All bot commands; persistent keyboard (MAIN_KEYBOARD), command list (COMMANDS),
                       /help, button router (on_button), /notify* rule management.
    guards.py        — is_allowed() — private chat only + user_id whitelist.

  services/
    stats.py         — get_daily_stats[_by_chat](), UTC↔local day boundaries, source-note (live/backfill).
    messages.py      — search_messages() — LIKE search with date/chat filters.
    jobs.py          — get_db_stats(), get_backfill_status(), get_backfill_days_stats().
    notifications.py — Keyword alerts: parse/compile patterns, STEMMED matching (RU/EN), rule CRUD,
                       active-rules cache, process_message() (match → annotate → forward to bot DM).

data/  (gitignored)  — telegram.sqlite3, telegram.session, days/*.jsonl, exports/*.jsonl
tests/               — pytest + pytest-asyncio (41 tests: db, guards, stats, notifications, bot menu)
```

**DB schema (4 tables):** `messages` (UNIQUE(chat_id, telegram_message_id), raw_json) · `ingest_state` (per-chat backfill status + title cache) · `backfill_days` (per-day completion) · `notification_rules` (name, pattern, is_active, include_archived).

### Conventions & patterns

- Async everywhere; both clients share one asyncio loop.
- `settings` is a **lazy proxy** (config.py) so importing any module never reads `.env` — keeps tests import-safe. Always `from app.config import settings`.
- SQLite via aiosqlite, WAL, `row_factory = Row`. Writes use `INSERT OR IGNORE` for idempotency. `db.py` owns the schema, but **`tests/conftest.py` mirrors it inline — keep both in sync** when adding tables/columns.
- Services (`app/services/`) take a `conn` and return plain dicts; they hold the logic and the unit tests. Bot handlers stay thin and delegate.
- Store timestamps in UTC; convert to `settings.tz` (default `Asia/Tbilisi`) only at day boundaries.

### Two identities (important)

- **Telethon user client** = your account. Sees all dialogs, can truly *forward* messages. Drives ingestion **and** notification delivery.
- **Control bot** (Bot API) = command surface only; it cannot read your chats or forward from them. Notifications are delivered *by the user client* into your DM with the bot.

### Gotchas (non-obvious)

- **PTB lifecycle is manual** (`async with app: start(); updater.start_polling()`), so PTB's `post_init` is **never called**. The native command menu is set by `bot.setup_commands()`, invoked explicitly in `main.py`. Put any startup-only bot setup there too.
- **Notifications run *before* the archived-skip** in `on_new_message`, so keyword alerts cover every chat regardless of archive/mute; ingestion may still skip archived chats when `SKIP_ARCHIVED=true`.
- **Keyword matching is morphological, not substring**: keywords and text are stemmed per word (snowballstemmer RU/EN, ё→е normalized) and compared as token stems — `сделать` fires on `сделай/сделаем/...`, but `кот` does *not* match `который`. Active rules are cached in `notifications._active_cache`; every rule mutation calls `invalidate_cache()`.
- **Reply keyboard ≠ command menu**: the persistent on-screen keyboard is a `ReplyKeyboardMarkup` attached per message (`handlers.MAIN_KEYBOARD`); the "Menu" button list is `setMyCommands` (`handlers.COMMANDS`). Both are wired in `bot/bot.py`.

### Run & test

```bash
python -m app.main     # start server (first run: enter the Telethon login code in the terminal)
pytest -q              # run the suite
```

CLI tools: `python -m app.backfill_from_export [result.json]` · `python -m app.style_analyzer`.

---

## Что это и зачем

headless-tgclient — личный локальный сервер для индексации всей твоей истории сообщений Telegram. Не альтернативный клиент, не бот для чужих пользователей — просто headless-процесс, который складывает все входящие и исходящие сообщения в SQLite-базу на твоей машине.

**Зачем:**
- Полный поиск по всей переписке за все годы — без лимитов Telegram-клиента
- Статистика и аналитика: сколько писал, кому, когда, о чём
- Стилистический анализ (скрипт `style_analyzer` строит профиль общения по всей истории)
- Экспорт своих сообщений для обработки — ML, LLM fine-tuning, построение «клона»
- Live-стриминг: все новые сообщения пишутся в БД в реальном времени — можно подключить внешний анализатор, триггеры, уведомления

**Что делает:**
- Авторизуется как пользователь через Telethon (MTProto) — видит всё, что видишь ты
- Пишет каждое сообщение в SQLite + дублирует в JSONL-файлы по дням
- Backfill: может загрузить историю за N дней через API или импортировать целиком из Telegram Desktop Data Export (`result.json`)
- Управление через отдельного Telegram-бота: статус, поиск, статистика, экспорт

**Хранение данных:**
- SQLite (WAL-режим) — одна база `telegram.sqlite3`, четыре таблицы: `messages`, `ingest_state`, `backfill_days`, `notification_rules`
- JSONL-лог по дням (`data/days/2026-05-27.jsonl`) — удобно для потокового чтения
- Экспорты (`data/exports/`) — отфильтрованные выгрузки только своих сообщений
- Всё лежит в `data/` — одна папка, легко бэкапить

**Приватность — всё локально:**
- Никакой контент не уходит ни в какое внешнее API. Нет HTTP-сервера, нет облака, нет аналитики
- Telethon подключается напрямую к серверам Telegram по MTProto — это тот же протокол, что и у обычного клиента
- Контрольный бот работает через Bot API, но только принимает команды от владельца — не пересылает сообщения наружу
- Рекомендуется запускать на домашнем ПК или личном сервере, а не в публичном облаке

**Что можно делать с данными дальше:**
- SQL-запросы по всей переписке прямо в базе (DBeaver, sqlite3, Python)
- Аналитика: активность по дням/часам, граф общения, частотный анализ
- Стилистический профиль → system prompt для LLM-клона (`data/style_profile.md`)
- Live-мониторинг: подключить watcher к БД или к daily JSONL для real-time анализа входящих сообщений
- Интеграция с любыми локальными пайплайнами — данные уже в удобных форматах (SQLite + JSONL)

---

## Architecture

```
┌────────────────────────────────────────────────────┐
│                    main.py                         │
│                                                    │
│  ┌──────────────┐         ┌──────────────────┐    │
│  │ Telethon      │         │ python-telegram- │    │
│  │ User Client   │         │ bot (Control)    │    │
│  │ (MTProto)     │         │ (Bot API)        │    │
│  └──────┬───────┘         └───────┬──────────┘    │
│         │ on_new_message          │ /commands      │
│         ▼                         ▼                │
│  ┌─────────────┐         ┌──────────────────┐     │
│  │ ingest.py    │         │ handlers.py      │     │
│  │ save_message │         │ backfill/export  │     │
│  └──────┬───────┘         └───────┬──────────┘    │
│         │                         │                │
│         ▼                         ▼                │
│  ┌──────────────────────────────────────────┐     │
│  │           SQLite (aiosqlite, WAL)         │     │
│  └──────────────────────────────────────────┘     │
│         │                                          │
│         ▼                                          │
│  ┌──────────────┐    ┌───────────────┐            │
│  │ data/days/   │    │ data/exports/ │            │
│  │ YYYY-MM-DD   │    │ my_messages   │            │
│  │ .jsonl       │    │ .jsonl        │            │
│  └──────────────┘    └───────────────┘            │
└────────────────────────────────────────────────────┘
```

Two clients run in one asyncio loop:
- **Telethon** (user client) receives all messages via MTProto, saves to DB + daily JSONL
- **python-telegram-bot** (control bot) provides commands for stats, search, backfill, export

## Features

### Live ingestion
All incoming/outgoing messages are saved in real-time. Archived chats are skipped from ingestion when `SKIP_ARCHIVED=true`. Each message is written to SQLite and appended to the daily JSONL file.

### Keyword notifications
Per-keyword alert rules. On a match, the **user client** forwards the original message into your DM with the control bot, prefixed with which trigger(s) fired. Managed entirely from the bot.

- Each rule has a name, an active/inactive flag, and a "watch archived chats" flag.
- Runs on **every** chat regardless of archive or Telegram mute state (evaluated before the ingestion archived-skip); incoming messages only. The owner↔bot DM (where alerts and command replies are delivered) is skipped, so a delivered alert or a `/notify` listing can't re-trigger itself.
- Pattern syntax: `,` = OR, `+` = AND (a "связка"). Example `дедлайн, отчёт + срочно` → fires on `дедлайн`, OR (`отчёт` AND `срочно`).
- **Morphology-aware** (snowballstemmer, RU/EN): keyword `сделать` also fires on `сделай`, `сделаем`, `сделаю`, …; `ё`/`е` are unified. Matching is per-word-stem, so `кот` does not match `который`.
- `/notify_add` and `/notify_edit <id>` are **sequential dialogs**: the bot asks for name → keywords → "watch archived chats" (inline Да/Нет) → "active now" (inline Да/Нет), one step per message. Edit shows the current value at each step; send `-` to keep it. Send `/cancel` to abort.
- Commands: `/notify` (list), `/notify_add`, `/notify_edit <id>`, `/notify_on|off <id>`, `/notify_del <id>`.

### Backfill
Historical message collection for the last N days (1–10000). Triggered via `/backfill N` + `/confirm_backfill`.

- Concurrent scanning (5 dialogs at a time)
- Filters: skips archived chats, skips chats with no messages from allowed users
- Filter results cached in `ingest_state` (subsequent runs skip filtering)
- Day-level tracking: already-completed days are skipped
- After completion, JSONL files are rebuilt from DB for data consistency
- Progress reported via bot at ~20% intervals

### Backfill from Data Export
For a full/large history import (faster and more complete than paging the API day-by-day), export your data from Telegram Desktop (Settings → Advanced → Export Telegram Data), then:

```bash
python -m app.backfill_from_export           # auto-detects DataExport_*/result.json
python -m app.backfill_from_export path.json  # explicit path
```

Processes the entire `result.json` (all chats, all messages), deduplicates against existing DB, marks days as completed, rebuilds daily JSONL logs. Idempotent — safe to re-run.

### Export
`/exportusershistory N` exports only the user's text messages for the last N days. Runs instantly from SQLite (no API calls). Output: `data/exports/my_messages_YYYY-MM-DD_Nd.jsonl`.

### Style Analyzer
Analyzes your writing style from exported messages and produces a comprehensive profile (system prompt for an LLM clone):

```bash
python -m app.style_analyzer
```

Output: `data/style_profile.md` (human-readable) + `data/style_profile.json` (raw data). Analyzes: message length, punctuation habits, vocabulary, slang frequency, emoji usage, greeting patterns, burst behavior, time-of-day activity, style evolution over years.

### Bot commands

| Command | Description |
|---|---|
| `/start` | Welcome + show the on-screen keyboard |
| `/help` | List all commands with descriptions |
| `/status` | Server status, DB size, connection state |
| `/stats YYYY-MM-DD` | Daily message counts (total/in/out) |
| `/stats_by_chat YYYY-MM-DD` | Per-chat breakdown for a day |
| `/dialogs` | Known dialogs sorted by message count |
| `/search query` | Text search across all messages |
| `/backfill N` | Start backfill for last N days |
| `/confirm_backfill` | Confirm and run pending backfill |
| `/exportusershistory N` | Export your messages for N days |
| `/notify` | List keyword-alert rules + usage |
| `/notify_add` | Add an alert rule (sequential dialog) |
| `/notify_edit <id>` | Edit an alert rule (sequential dialog) |
| `/notify_on \| off <id>` | Enable / disable a rule |
| `/notify_del <id>` | Delete a rule |

**UI:** a persistent on-screen keyboard (📊 Статус · 💬 Диалоги · 📅 Статистика · 🔔 Уведомления · 📖 Команды) gives one-tap access to the main functions, and the native Telegram "Menu" button lists all commands (published via `setMyCommands` on startup).

Bot access is restricted to `ALLOWED_TELEGRAM_USER_IDS` in private chat only.

---

## Setup

### Prerequisites
- Python 3.12+
- Windows (tested on Windows 11)
- Telegram account with API credentials
- Telegram bot (via @BotFather)

### 1. Get Telegram API credentials

Go to https://my.telegram.org/apps, create an application, note `api_id` and `api_hash`.

### 2. Create a control bot

Message @BotFather on Telegram, `/newbot`, save the token.

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
TG_API_ID=your_api_id
TG_API_HASH=your_api_hash
TG_PHONE=+your_phone_number
TG_2FA_PASSWORD=your_2fa_password    # optional, only if 2FA is enabled

CONTROL_BOT_TOKEN=your_bot_token
ALLOWED_TELEGRAM_USER_IDS=your_telegram_user_id

LOCAL_TIMEZONE=Asia/Tbilisi           # your local timezone
SKIP_ARCHIVED=true                    # skip archived chats in live + backfill
LOG_LEVEL=INFO
```

To find your Telegram user ID, message @userinfobot.

### 4. Install

```bash
pip install -e ".[dev]"
```

### 5. First run

```bash
python -m app.main
```

On first launch, Telethon will ask for the auth code sent to your Telegram. Enter it in the terminal. If 2FA is enabled, set `TG_2FA_PASSWORD` in `.env`.

### 6. Run tests

```bash
pytest tests/ -v
```

---

## Data storage

| Location | Content | Format | Write mode |
|---|---|---|---|
| `data/telegram.sqlite3` | All messages + metadata | SQLite WAL | INSERT OR IGNORE |
| `data/days/YYYY-MM-DD.jsonl` | All messages for a day | JSONL | Append (live) / Rewrite (backfill) |
| `data/exports/my_messages_*.jsonl` | User's outgoing text messages | JSONL | Rewrite per export |
| `data/telegram.session` | Telethon auth session | SQLite | Managed by Telethon |

### JSONL record format (daily log)

```json
{
  "message_id": 12345,
  "chat_id": -100123456,
  "chat_title": "Chat Name",
  "sender_id": 100000000,
  "sender_name": "User",
  "outgoing": true,
  "date_utc": "2026-05-27T10:30:00+00:00",
  "date_local": "2026-05-27T14:30:00+04:00",
  "text": "message text",
  "has_media": false,
  "media_type": null
}
```

---

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TG_API_ID` | yes | | Telegram API app ID |
| `TG_API_HASH` | yes | | Telegram API app hash |
| `TG_PHONE` | yes | | Phone number for auth |
| `TG_2FA_PASSWORD` | no | `null` | Two-factor auth password |
| `TG_SESSION` | no | `data/telegram.session` | Telethon session file path |
| `CONTROL_BOT_TOKEN` | yes | | Telegram bot token |
| `ALLOWED_TELEGRAM_USER_IDS` | yes | | Comma-separated user IDs |
| `DB_PATH` | no | `data/telegram.sqlite3` | SQLite database path |
| `LOCAL_TIMEZONE` | no | `Asia/Tbilisi` | Timezone for daily boundaries |
| `SKIP_ARCHIVED` | no | `true` | Skip archived chats everywhere |
| `LOG_LEVEL` | no | `INFO` | Logging level |
