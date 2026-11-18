# headless-tgclient

Headless Telegram recorder for work chats. Logs in as your user via MTProto, writes new messages from the chosen Telegram folder (e.g. `WTP`) into SQLite, and sends keyword alerts through a control bot. Runs as a background service on a Linux server (or locally on Windows). No public API.

---

## 🤖 LLM agent guide (read this first)

**What this is:** a personal Telegram recorder. A Telethon *user client* writes every new message from the whitelisted folder into SQLite; a separate *control bot* (python-telegram-bot) shows status and manages notification rules. No HTTP API — by design. Scope is deliberately small: record history + a rule-based notification framework. History backfill, exports and analytics were removed; reading the history from outside (MCP) comes later.

**Stack:** Python 3.12+ (runs on 3.14), Telethon (MTProto), python-telegram-bot 22.x, aiosqlite (WAL), pydantic-settings, snowballstemmer. One process, one asyncio loop hosting both clients.

**Start reading at** [`app/main.py`](app/main.py) → `_run()`: logging, Telethon start (non-interactive under systemd), folder whitelist, the `on_new_message` live handler, the maintenance loop (folder refresh + storage cap), then the control-bot lifecycle.

### File map

```
app/
  main.py            — Entry point (_run). Startup, terminal UI, live handler (on_new_message),
                       notification dispatch + forward target, archived-chat cache, maintenance loop.
  config.py          — Pydantic Settings from .env via a LAZY proxy (settings).
  db.py              — SQLite schema (messages, notification_rules) + init_db()/get_connection(). WAL.
  telegram_client.py — Telethon create/start; SessionNotAuthorized when no TTY and no valid session.
  login.py           — Interactive login CLI (python -m app.login) → data/telegram.session;
                       --query logs in the separate query session.
  query.py           — Read-only CLI over Telegram itself (python -m app.query): folder chats,
                       search, history. Own session, runs next to the service.
  folders.py         — ChatWhitelist: chat ids of the CHAT_FOLDERS Telegram folder(s).
  ingest.py          — save_message() — INSERT OR IGNORE into messages.
  util.py            — full_name(), day_bounds_utc().

  bot/
    bot.py           — create_bot_app(): registers handlers; setup_commands() sets the "Menu" list.
    handlers.py      — /start, /help, /status, keyboard (MAIN_KEYBOARD), COMMANDS, /notify* rules.
    guards.py        — is_allowed() — private chat only + owner user_id.

  services/
    jobs.py          — get_db_stats().
    retention.py     — storage cap: prune oldest local days while the DB exceeds MAX_STORAGE_MB.
    notifications.py — Keyword alerts: parse/compile patterns, STEMMED matching (RU/EN), rule CRUD,
                       active-rules cache, process_message() (match → annotate → forward to bot DM).

deploy/              — systemd unit + deploy.sh (tar over ssh, rsync into /opt/tgclient, restart).
data/  (gitignored)  — telegram.sqlite3, telegram.session
tests/               — pytest + pytest-asyncio
```

**DB schema (2 tables):** `messages` (UNIQUE(chat_id, telegram_message_id), raw_json) · `notification_rules` (name, pattern, is_active, include_archived).

### Conventions & patterns

- Async everywhere; both clients share one asyncio loop.
- `settings` is a **lazy proxy** (config.py) so importing any module never reads `.env` — keeps tests import-safe. Always `from app.config import settings`.
- SQLite via aiosqlite, WAL, `row_factory = Row`. Writes use `INSERT OR IGNORE` for idempotency. `db.py` owns the schema, but **`tests/conftest.py` mirrors it inline — keep both in sync** when adding tables/columns.
- Services (`app/services/`) take a `conn` and return plain dicts; they hold the logic and the unit tests. Bot handlers stay thin and delegate.
- Store timestamps in UTC; convert to `settings.tz` (default `Asia/Tbilisi`) only at day boundaries.

### Two identities (important)

- **Telethon user client** = your account. Sees all dialogs, can truly *forward* messages. Drives ingestion and the **forward** half of notification delivery.
- **Control bot** (Bot API) = command surface; it can't read your chats or forward from them, but it **can DM you**. So an alert is delivered in two parts: the **bot** sends the trigger text (a message *from* the bot, so Telegram gives you a real push notification) and the **user client** forwards the original message into the bot DM for full content.

### Gotchas (non-obvious)

- **Folder whitelist gates everything.** With `CHAT_FOLDERS` set, `on_new_message` returns early for chats outside the folder — before notifications. It fails closed: folder not found → nothing is processed.
- **PTB lifecycle is manual** (`async with app: start(); updater.start_polling()`), so PTB's `post_init` is **never called**. The native command menu is set by `bot.setup_commands()`, invoked explicitly in `main.py`.
- **Keyword matching is morphological, not substring**: keywords and text are stemmed per word (snowballstemmer RU/EN, ё→е normalized) and compared as token stems — `сделать` fires on `сделай/сделаем/...`, but `кот` does *not* match `который`. Active rules are cached in `notifications._active_cache`; every rule mutation calls `invalidate_cache()` (a rule written straight into the DB needs a restart).
- **Reply keyboard ≠ command menu**: the persistent on-screen keyboard is a `ReplyKeyboardMarkup` attached per message (`handlers.MAIN_KEYBOARD`); the "Menu" button list is `setMyCommands` (`handlers.COMMANDS`). Both are wired in `bot/bot.py`.

### Run & test

```bash
python -m app.main     # start (in a terminal it asks for the login code on first run)
python -m app.login    # log in / refresh the session explicitly
pytest -q              # run the suite
```

---

## Что это и зачем

Фоновый сервер, который пишет историю рабочих чатов Telegram. Работает от имени вашего аккаунта, но смотрит только в чаты из выбранной папки (например, `WTP`) и складывает новые сообщения в SQLite.

**Что делает сейчас:**
- Пишет каждое новое сообщение из папки в базу — входящие и исходящие, с raw JSON
- Держит базу в пределах лимита (`MAX_STORAGE_MB`): самые старые дни удаляются
- Уведомления по правилам: при срабатывании бот присылает пуш и пересылает оригинал
- Управление через отдельного бота: статус и правила уведомлений

**Что планируется:** доступ к истории извне по MCP (только чтение, через SSH), сводка за день, напоминания о задачах из чатов.

---

## Features

### Live ingestion
New messages (incoming and outgoing) from whitelisted chats are saved in real time. With `CHAT_FOLDERS` set, a chat is processed iff it is in the folder (archived or not); without it, every chat is processed and archived ones are skipped when `SKIP_ARCHIVED=true`. History before the server started is not loaded.

### Folder whitelist
`CHAT_FOLDERS=WTP` — the folder is read from Telegram at startup, on every folder change and every 10 minutes; adding a chat to the folder in any Telegram app is enough. Folders defined by categories (groups, channels, contacts, bots) are supported; "exclude muted/read" conditions are ignored.

### Storage cap
`MAX_STORAGE_MB` — checked at startup and every 10 minutes. Over the limit, whole oldest local days are deleted until usage drops to 90% of it. SQLite reuses freed pages, so the file stays around the limit.

### Keyword notifications
Per-keyword alert rules. On a match you get two messages in your DM with the control bot: the **bot** posts which trigger(s) fired (a real push notification), and the **user client** forwards the original message right after it.

- Each rule has a name, an active/inactive flag, and a "watch archived chats" flag.
- Incoming messages in whitelisted chats only. The owner↔bot DM is skipped, so a delivered alert or a `/notify` listing can't re-trigger itself.
- Pattern syntax: `,` = OR, `+` = AND. Example `дедлайн, отчёт + срочно` → fires on `дедлайн`, OR (`отчёт` AND `срочно`).
- **Morphology-aware** (snowballstemmer, RU/EN): `сделать` also fires on `сделай`, `сделаем`, …; `ё`/`е` are unified; `кот` does not match `который`.
- `/notify_add` and `/notify_edit <id>` are sequential dialogs: name → keywords → "watch archived chats" → "active now". Edit shows the current value; send `-` to keep it, `/cancel` to abort.

### Bot commands

| Command | Description |
|---|---|
| `/start` | Welcome + show the on-screen keyboard |
| `/help` | List all commands with descriptions |
| `/status` | Connection, whitelisted chats, storage vs. limit, message count |
| `/notify` | List alert rules + usage |
| `/notify_add` | Add an alert rule (sequential dialog) |
| `/notify_edit <id>` | Edit an alert rule (sequential dialog) |
| `/notify_on \| off <id>` | Enable / disable a rule |
| `/notify_del <id>` | Delete a rule |

**UI:** a persistent on-screen keyboard (📊 Статус · 🔔 Уведомления · 📖 Команды) and the native Telegram "Menu" button (published via `setMyCommands` on startup).

Bot access is restricted to the single `ALLOWED_TELEGRAM_USER_ID` in private chat only.

---

## Setup

### Prerequisites
- Python 3.12+
- Linux server (see "Server deployment") or Windows for local runs
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

Fill in the credentials and the owner id (message @userinfobot to find yours). See "Configuration reference" for the rest.

### 4. Install

```bash
pip install -e ".[dev]"
```

### 5. Log in and run

```bash
python -m app.login
python -m app.main
```

The login code arrives in your Telegram app; type it into the terminal. If 2FA is enabled, set `TG_2FA_PASSWORD` in `.env`.

`app.main` asks for a code only when stdin is a terminal. Without one (systemd) it never requests a code: if the session is missing or revoked it alerts the owner via the bot and exits with status 78.

### 6. Run tests

```bash
pytest -q
```

---

## Data storage

| Location | Content | Format |
|---|---|---|
| `data/telegram.sqlite3` | Messages + notification rules | SQLite WAL, INSERT OR IGNORE |
| `data/telegram.session` | Telethon auth session | Managed by Telethon |
| `data/query.session` | Second login, used only by `app.query` | Managed by Telethon |

---

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TG_API_ID` | yes | | Telegram API app ID |
| `TG_API_HASH` | yes | | Telegram API app hash |
| `TG_PHONE` | yes | | Phone number for auth |
| `TG_2FA_PASSWORD` | no | `null` | Two-factor auth password |
| `TG_SESSION` | no | `data/telegram.session` | Telethon session file path |
| `TG_QUERY_SESSION` | no | `data/query.session` | Session file of the separate login used by `app.query` |
| `CONTROL_BOT_TOKEN` | yes | | Telegram bot token |
| `ALLOWED_TELEGRAM_USER_ID` | yes | | Single owner user ID |
| `DB_PATH` | no | `data/telegram.sqlite3` | SQLite database path |
| `LOCAL_TIMEZONE` | no | `Asia/Tbilisi` | Timezone for daily boundaries |
| `CHAT_FOLDERS` | no | empty | Telegram folder names, comma-separated. Only their chats are stored and scanned for notifications; empty = every chat |
| `SKIP_ARCHIVED` | no | `true` | Skip archived chats (only when `CHAT_FOLDERS` is empty) |
| `MAX_STORAGE_MB` | no | `0` | DB budget; over it the oldest days are pruned down to 90%. `0` = unlimited |
| `LOG_LEVEL` | no | `INFO` | Logging level |
| `LOG_FILE` | no | `logs/tgclient.log` | Rotating log file (10 MB × 3); empty = stdout only |

---

## Server deployment

Linux + systemd, as a dedicated user, code in `/opt/tgclient`. Files: [`deploy/tgclient.service`](deploy/tgclient.service), [`deploy/deploy.sh`](deploy/deploy.sh).

**One-time setup (as root on the server):**

```bash
useradd --system --home-dir /opt/tgclient --no-create-home --shell /usr/sbin/nologin tgclient
mkdir -p /opt/tgclient/data /opt/tgclient/logs
```

Then from the repo root on your machine: `bash deploy/deploy.sh moskwin` (copies the code, creates the venv; it skips the restart while the unit isn't installed).

**`.env` on the server:** copy your `.env`, add the server profile from `.env.example` (`CHAT_FOLDERS=WTP`, `MAX_STORAGE_MB=5120`, `LOG_FILE=`), then:

```bash
chown tgclient:tgclient /opt/tgclient/.env && chmod 600 /opt/tgclient/.env
```

**Login (OTP code):** over SSH, in a terminal:

```bash
cd /opt/tgclient && sudo -u tgclient .venv/bin/python -m app.login
```

The code arrives in your Telegram app; type it into the SSH session. This creates a separate session (a new "device" in Telegram settings). Never send the code through a Telegram chat: Telegram expires codes shared in messages. Repeat this only if the session gets revoked; the bot will tell you.

**Service:**

```bash
tr -d '\r' < /opt/tgclient/deploy/tgclient.service > /etc/systemd/system/tgclient.service
systemctl daemon-reload && systemctl enable --now tgclient
journalctl -u tgclient -f
```

**Querying Telegram directly (no service stop, not the DB):** `app.query` reads the folder chats straight from Telegram through its own login, so it can run while the service is up. Log in once, then query:

```bash
cd /opt/tgclient && sudo -u tgclient .venv/bin/python -m app.login --query
sudo -u tgclient .venv/bin/python -m app.query chats
sudo -u tgclient .venv/bin/python -m app.query search "договор" --since 2026-11-01 --limit 10
sudo -u tgclient .venv/bin/python -m app.query search "AML" --count
sudo -u tgclient .venv/bin/python -m app.query history --chat valut --limit 30
sudo -u tgclient .venv/bin/python -m app.query history --chat valut --around 12345
```

If the login code never arrives, add `--qr` to the login command and scan the QR code in the Telegram app (Settings → Devices → Link Desktop Device); it works for the service session too. Only chats of `CHAT_FOLDERS` are visible. Search is Telegram's own (whole words and prefixes, no morphology). `--from` and `--since` are filtered client-side over at most 3000 messages per chat. The script never writes to Telegram or the DB; the session file itself is a full login, so keep it as private as `telegram.session`.

**Updates:** `bash deploy/deploy.sh moskwin` (code only: `data/`, `logs/`, `.env` are never touched; reinstall; restart).

**Only one instance at a time:** the same bot token polled from two places conflicts. Don't run the local copy while the server one is up.
