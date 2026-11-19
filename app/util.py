from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def full_name(entity) -> str | None:
    """First + last name of a user-like entity, or None if both are empty."""
    if entity is None:
        return None
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return f"{first} {last}".strip() or None


def local_time(iso_utc: str | None, tz: ZoneInfo) -> str:
    """A stored UTC ISO timestamp as local 'YYYY-MM-DD HH:MM' for display."""
    if not iso_utc:
        return "—"
    return datetime.fromisoformat(iso_utc).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def day_bounds_utc(date_str: str, tz: ZoneInfo) -> tuple[str, str]:
    """UTC ISO [start, end) covering one local calendar day (YYYY-MM-DD)."""
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()

