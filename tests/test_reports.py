from datetime import date, datetime
from zoneinfo import ZoneInfo

from newapi_watch.config import Settings
from newapi_watch.models import LogRecord
from newapi_watch.reports import Reports, day_window
from newapi_watch.store import Store


def test_report_dst_window_is_local_calendar_day():
    start, end = day_window(date(2026, 11, 1), "America/New_York")
    assert end - start == 25 * 3600
    assert datetime.fromtimestamp(start, ZoneInfo("America/New_York")).hour == 0


async def test_report_no_data_explicitly_marks_incomplete_and_png(tmp_path):
    store = Store(tmp_path / "db")
    await store.open()
    try:
        reports = Reports(Settings(_env_file=None, demo=True), store)
        result = await reports.daily(date(2026, 11, 9))
        assert "不完整" in result.text
        assert result.image.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        await store.close()


async def test_report_aggregates_exact_quota_without_invented_currency(tmp_path):
    store = Store(tmp_path / "db")
    await store.open()
    try:
        start, end = day_window(date(2026, 11, 9), "Asia/Shanghai")
        await store.save_logs(
            [
                LogRecord(id=1, created_at=start, type=2, quota=900, prompt_tokens=10),
                LogRecord(id=2, created_at=end - 1, type=5),
                LogRecord(id=3, created_at=end, type=2, quota=999),
            ],
            end,
            start,
        )
        result = await Reports(Settings(_env_file=None, demo=True), store).daily(date(2026, 11, 9))
        assert "900" in result.text
        assert "成功记录：1" in result.text
        assert "错误记录：1" in result.text
        assert "完整" in result.text
        assert "美元" not in result.text
    finally:
        await store.close()
