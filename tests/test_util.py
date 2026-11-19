from zoneinfo import ZoneInfo

from app.util import local_time

TBILISI = ZoneInfo("Asia/Tbilisi")


def test_local_time_converts_utc_to_local():
    assert local_time("2026-11-19T06:15:07+00:00", TBILISI) == "2026-11-19 10:15"


def test_local_time_crosses_midnight():
    assert local_time("2026-11-19T21:30:00+00:00", TBILISI) == "2026-11-20 01:30"


def test_local_time_empty():
    assert local_time(None, TBILISI) == "—"
