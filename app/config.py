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

    tg_2fa_password: str | None = None

    control_bot_token: str
    allowed_telegram_user_ids: list[int]

    @field_validator("allowed_telegram_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    db_path: Path = _PROJECT_DIR / "data" / "telegram.sqlite3"
    local_timezone: str = "Asia/Tbilisi"

    skip_archived: bool = True

    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.local_timezone)


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class _LazySettings:
    """Proxy that defers Settings construction until first attribute access."""

    _instance: Settings | None = None

    def _load(self) -> Settings:
        if self._instance is None:
            self._instance = get_settings()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


settings: Settings = _LazySettings()  # type: ignore[assignment]
