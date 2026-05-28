# headless-tgclient

Personal headless Telegram client for Windows. Authorizes as a user via MTProto, indexes messages into SQLite, provides control via Telegram bot. Runs locally, no public API.

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
- SQLite (WAL-режим) — одна база `telegram.sqlite3`, три таблицы: `messages`, `ingest_state`, `backfill_days`
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

## LLM Navigation

```
app/
  main.py            — Entry point. Startup, ANSI terminal UI, live message handler (on_new_message),
                       archived-chat cache, missing-title repair, signal handling.
  config.py          — Pydantic Settings from .env. Lazy proxy (settings). Key fields: tg_*, control_bot_token,
                       allowed_telegram_user_ids, skip_archived, local_timezone.
  db.py              — SQLite schema (messages, ingest_state, backfill_days). WAL mode. init_db() + get_connection().
  telegram_client.py — Telethon client create/start. Supports 2FA via tg_2fa_password.
  ingest.py          — save_message() — INSERT OR IGNORE into messages. Returns bool (inserted or duplicate).
  backfill.py        — run_backfill(client, conn, days, tz) — concurrent dialog scan (5 workers),
                       day-level tracking, user-participation filter with caching.
                       Key helpers: _dialog_title(), _backfill_dialog(), _date_range_utc_bounds().
  daily_log.py       — JSONL daily files in data/days/. append_message(), rebuild_day_from_db().
  export.py          — export_my_messages(conn, days, tz) — SQL query from DB, outputs data/exports/*.jsonl.
  backfill_from_export.py — Import entire history from Telegram Desktop Data Export (result.json).
                       CLI: python -m app.backfill_from_export [path]. Batch INSERT OR IGNORE, marks days, rebuilds JSONL.
  style_analyzer.py   — Analyze user's writing style from exported messages. Produces style_profile.md + .json.
                       CLI: python -m app.style_analyzer [--input FILE] [--output FILE].

  bot/
    bot.py           — create_bot_app(). Registers all command handlers. Stores tg_client + db_conn in bot_data.
    handlers.py      — Bot commands: /status, /stats, /stats_by_chat, /dialogs, /search,
                       /backfill + /confirm_backfill, /exportusershistory.
    guards.py        — is_allowed() — private chat only + user_id whitelist.

  services/
    stats.py         — get_daily_stats(), get_daily_stats_by_chat(). UTC boundary conversion.
    messages.py      — search_messages() — LIKE-based text search with date/chat filters.
    jobs.py          — get_db_stats(), get_backfill_status(), get_backfill_days_stats().

data/                — Runtime data (gitignored)
  telegram.sqlite3   — Main DB
  telegram.session   — Telethon session file
  days/              — Daily JSONL files (all messages per day)
  exports/           — User message exports (outgoing only, text-only)

tests/               — pytest + pytest-asyncio, 12 tests (db, guards, stats)
```

**DB schema (3 tables):**
- `messages` — UNIQUE(chat_id, telegram_message_id), stores raw_json
- `ingest_state` — per-chat backfill status + title cache
- `backfill_days` — per-day completion tracking

**Key patterns:** INSERT OR IGNORE for idempotency, asyncio.Semaphore(5) for concurrent API calls, asyncio.Lock for SQLite writes, Pydantic lazy proxy to avoid import-time .env loading in tests.

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
All incoming/outgoing messages are saved in real-time. Archived chats are skipped when `SKIP_ARCHIVED=true`. Each message is written to SQLite and appended to the daily JSONL file.

### Backfill
Historical message collection for the last N days (1-365). Triggered via `/backfill N` + `/confirm_backfill`.

- Concurrent scanning (5 dialogs at a time)
- Filters: skips archived chats, skips chats with no messages from allowed users
- Filter results cached in `ingest_state` (subsequent runs skip filtering)
- Day-level tracking: already-completed days are skipped
- After completion, JSONL files are rebuilt from DB for data consistency
- Progress reported via bot at ~20% intervals

### Backfill from Data Export
For full history import (beyond the 365-day API limit), export your data from Telegram Desktop (Settings → Advanced → Export Telegram Data), then:

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
| `/status` | Server status, DB size, connection state |
| `/stats YYYY-MM-DD` | Daily message counts (total/in/out) |
| `/stats_by_chat YYYY-MM-DD` | Per-chat breakdown for a day |
| `/dialogs` | Known dialogs sorted by message count |
| `/search query` | Text search across all messages |
| `/backfill N` | Start backfill for last N days |
| `/confirm_backfill` | Confirm and run pending backfill |
| `/exportusershistory N` | Export your messages for N days |

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
