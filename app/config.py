from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings


_PROJECT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = {"env_file": _PROJECT_DIR / ".env", "env_file_encoding": "utf-8"}

    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session: Path = _PROJECT_DIR / "data" / "telegram.session"

    # Separate login used by `python -m app.query`, so ad-hoc queries can run
    # while the service holds tg_session.
    tg_query_session: Path = _PROJECT_DIR / "data" / "query.session"

    tg_2fa_password: str | None = None

    control_bot_token: str
    allowed_telegram_user_id: int

    @field_validator("allowed_telegram_user_id", mode="before")
    @classmethod
    def parse_user_id(cls, v):
        if isinstance(v, str):
            parts = [x.strip() for x in v.split(",") if x.strip()]
            if len(parts) != 1:
                raise ValueError(
                    "ALLOWED_TELEGRAM_USER_ID must contain exactly one user id"
                )
            return int(parts[0])
        return v

    db_path: Path = _PROJECT_DIR / "data" / "telegram.sqlite3"
    local_timezone: str = "Asia/Tbilisi"

    skip_archived: bool = True

    # Comma-separated Telegram folder names (e.g. "WTP"). When set, only chats
    # in these folders are stored and scanned for notifications. Empty = every chat.
    chat_folders: str = ""

    # Storage budget for the DB, in MB. Over budget, the oldest local days are
    # pruned. 0 = unlimited.
    max_storage_mb: int = 0

    log_level: str = "INFO"
    # Log file, relative to the project dir. Empty = stdout only (systemd/journald).
    log_file: str = "logs/tgclient.log"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone)

    @property
    def chat_folder_names(self) -> list[str]:
        return [x.strip() for x in self.chat_folders.split(",") if x.strip()]

    @property
    def log_path(self) -> Path | None:
        if not self.log_file.strip():
            return None
        path = Path(self.log_file)
        return path if path.is_absolute() else _PROJECT_DIR / path


class _LazySettings:
    """Proxy that defers Settings construction until first attribute access."""

    _instance: Settings | None = None

    def _load(self) -> Settings:
        if self._instance is None:
            self._instance = Settings()  # type: ignore[call-arg]
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


settings: Settings = _LazySettings()  # type: ignore[assignment]
