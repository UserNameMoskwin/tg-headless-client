from __future__ import annotations

from zoneinfo import ZoneInfo

from app.services import retention
from tests.conftest import insert_test_message

TZ = ZoneInfo("Asia/Tbilisi")  # UTC+4
BIG = "x" * 20_000


async def _fill(conn, days: list[str], per_day: int = 10) -> None:
    msg_id = 0
    for day in days:
        for i in range(per_day):
            msg_id += 1
            await insert_test_message(
                conn, msg_id=msg_id, date_utc=f"{day}T10:{i:02d}:00+00:00", text=BIG,
            )


async def _days_left(conn) -> list[str]:
    rows = await conn.execute_fetchall(
        "SELECT DISTINCT substr(message_date_utc, 1, 10) FROM messages ORDER BY 1"
    )
    return [r[0] for r in rows]


async def test_under_limit_is_noop(db_conn):
    await _fill(db_conn, ["2026-05-01", "2026-05-02"])
    used = await retention.storage_used_bytes(db_conn)
    result = await retention.enforce_storage_limit(db_conn, used * 2, TZ)
    assert result["days_deleted"] == []
    assert await _days_left(db_conn) == ["2026-05-01", "2026-05-02"]


async def test_zero_limit_means_unlimited(db_conn):
    await _fill(db_conn, ["2026-05-01"])
    result = await retention.enforce_storage_limit(db_conn, 0, TZ)
    assert result["days_deleted"] == []


async def test_prunes_oldest_days_first(db_conn):
    days = ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"]
    await _fill(db_conn, days)
    used = await retention.storage_used_bytes(db_conn)

    # budget for roughly 2.5 days of data → oldest days go, newest stay
    result = await retention.enforce_storage_limit(db_conn, int(used * 0.6), TZ)

    left = await _days_left(db_conn)
    assert result["days_deleted"][0] == "2026-05-01"
    assert "2026-05-04" in left
    assert "2026-05-01" not in left
    assert result["used_after"] <= int(used * 0.6)
    assert result["messages_deleted"] == 10 * len(result["days_deleted"])
