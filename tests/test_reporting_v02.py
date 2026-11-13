from datetime import date, datetime

import pytest

from newapi_watch.config import Settings
from newapi_watch.models import LogRecord
from newapi_watch.periods import StatisticsQuery
from newapi_watch.reports import Reports
from newapi_watch.store import Store


@pytest.fixture
async def reports(tmp_path):
    settings = Settings(_env_file=None, demo=True, timezone="UTC")
    store = Store(tmp_path / "reports.sqlite3")
    await store.open()
    start = int(datetime.fromisoformat("2026-11-08T00:00:00+00:00").timestamp())
    await store.save_logs(
        [
            LogRecord(
                id=1,
                created_at=start,
                type=2,
                channel=7,
                user_id=9,
                model_name="model-a",
                quota=125,
            ),
            LogRecord(
                id=2,
                created_at=start + 86400,
                type=2,
                channel=8,
                user_id=10,
                model_name="model-b",
                quota=875,
            ),
        ],
        start + 172800,
        start,
    )
    try:
        yield Reports(settings, store)
    finally:
        await store.close()


async def test_multi_day_report_contains_rank_labels_and_png(reports):
    result = await reports.range_report(
        StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 9), dimension="model")
    )
    assert "1000" in result.text and "model-b" in result.text
    assert "2026-11-08" in result.text and "2026-11-09" in result.text
    assert "非货币" in result.text and "最终请求" in result.text
    assert result.image.startswith(b"\x89PNG\r\n\x1a\n")


async def test_ranking_image_includes_no_raw_names(reports):
    result = await reports.range_report(
        StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 9), dimension="user"),
        chart_kind="ranking",
    )
    assert "用户 #10" in result.text
    assert result.image.startswith(b"\x89PNG\r\n\x1a\n")


async def test_daily_includes_model_channel_leaders_and_alert_status(reports):
    result = await reports.daily(date(2026, 11, 9), with_image=False)
    assert "model-b" in result.text
    assert "渠道 #8" in result.text
    assert "当前告警" in result.text
    assert result.image is None


async def test_chart_capacity_stays_reserved_until_cancelled_render_finishes(reports, monkeypatch):
    import asyncio
    import threading

    from matplotlib.backends.backend_agg import FigureCanvasAgg

    from newapi_watch.operations import OperationError

    reports.settings.bot_concurrency = 1
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    original = FigureCanvasAgg.print_png

    def blocking(canvas, *args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(10):
            raise RuntimeError("test renderer was not released")
        return original(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, "print_png", blocking)
    first = asyncio.create_task(reports.chart(date(2026, 11, 9)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        first.cancel()
        processed = asyncio.Event()
        loop.call_soon(processed.set)
        await processed.wait()
        async with asyncio.timeout(1):
            with pytest.raises(OperationError) as busy:
                await reports.chart(date(2026, 11, 9))
            assert busy.value.status == 429
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
