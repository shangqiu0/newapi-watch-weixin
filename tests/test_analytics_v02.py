from datetime import date, datetime

import pytest
from pydantic import ValidationError

from newapi_watch.analytics import Analytics
from newapi_watch.models import LogRecord
from newapi_watch.periods import Period, StatisticsQuery, parse_period
from newapi_watch.store import Store


def ts(value):
    return int(datetime.fromisoformat(value).timestamp())


@pytest.fixture
async def store(tmp_path):
    db = Store(tmp_path / "analytics.sqlite3")
    await db.open()
    try:
        yield db
    finally:
        await db.close()


def test_period_aliases_and_inclusive_calendar_ranges():
    today = date(2026, 11, 10)
    assert parse_period("近7天", today=today) == Period(start=date(2026, 11, 4), end=today)
    assert parse_period("昨天", today=today).start == date(2026, 11, 9)
    assert parse_period("2026-11-01~2026-11-03", today=today).days == 3


@pytest.mark.parametrize(
    "text", ["近0天", "近32天", "2026-11-11", "2026-11-10~2026-11-09", "invalid"]
)
def test_period_invalid_or_future_is_rejected(text):
    with pytest.raises(ValueError):
        parse_period(text, today=date(2026, 11, 10))


@pytest.mark.parametrize(
    "values",
    [
        {"dimension": "quota; DROP TABLE logs"},
        {"limit": 21},
        {"metric": "password"},
        {"channel_id": -1},
    ],
)
def test_statistics_input_whitelists(values):
    with pytest.raises(ValidationError):
        StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 9), **values)


async def test_snapshot_exact_totals_rankings_and_exclusive_end(store):
    start, end = ts("2026-11-08T00:00:00+00:00"), ts("2026-11-10T00:00:00+00:00")
    await store.save_logs(
        [
            LogRecord(
                id=1,
                created_at=start,
                type=2,
                channel=1,
                user_id=1,
                model_name="a",
                quota=100,
                prompt_tokens=10,
                completion_tokens=20,
            ),
            LogRecord(
                id=2,
                created_at=start + 86400,
                type=2,
                channel=2,
                user_id=2,
                model_name="b",
                quota=300,
                prompt_tokens=30,
                completion_tokens=40,
            ),
            LogRecord(
                id=3, created_at=start + 5, type=5, channel=1, user_id=1, model_name="a", quota=999
            ),
            LogRecord(id=4, created_at=start + 6, type=1, quota=9000),
            LogRecord(id=5, created_at=end, type=2, quota=10000),
        ],
        end,
        start,
    )
    query = StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 9), dimension="model")
    report = await Analytics(store, "UTC").snapshot(query)
    assert report.summary.success_records == 2
    assert report.summary.error_records == 1
    assert report.summary.quota == 400
    assert report.summary.tokens == 100
    assert report.summary.active_users == 2
    assert report.summary.active_channels == 2
    assert [(item.label, item.quota) for item in report.ranking] == [("b", 300), ("a", 100)]
    assert [(item.label, item.quota) for item in report.series] == [
        ("2026-11-08", 100),
        ("2026-11-09", 300),
    ]
    assert report.coverage.complete is True


async def test_channel_filter_error_ordering_and_empty_day_zero_fill(store):
    start, end = ts("2026-11-07T00:00:00+00:00"), ts("2026-11-10T00:00:00+00:00")
    await store.save_logs(
        [
            LogRecord(id=1, created_at=start + 86400, type=2, channel=1, user_id=3, quota=80),
            LogRecord(id=2, created_at=start + 86401, type=5, channel=1, user_id=3),
            LogRecord(id=3, created_at=start + 86402, type=2, channel=2, user_id=4, quota=900),
        ],
        end,
        start,
    )
    report = await Analytics(store, "UTC").snapshot(
        StatisticsQuery(
            start=date(2026, 11, 7),
            end=date(2026, 11, 9),
            dimension="user",
            metric="errors",
            channel_id=1,
        )
    )
    assert report.summary.quota == 80
    assert [item.quota for item in report.series] == [0, 80, 0]
    assert report.ranking[0].key == "3"
    assert report.ranking[0].error_records == 1


async def test_half_hour_timezone_never_leaks_adjacent_calendar_day(store):
    start, end = ts("2026-11-08T00:00:00+05:45"), ts("2026-11-09T00:00:00+05:45")
    await store.save_logs(
        [
            LogRecord(id=1, created_at=start, type=2, quota=10),
            LogRecord(id=2, created_at=end - 1, type=2, quota=20),
            LogRecord(id=3, created_at=end, type=2, quota=999),
        ],
        end,
        start,
    )
    report = await Analytics(store, "Asia/Kathmandu").snapshot(
        StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 8))
    )
    assert len(report.series) == 24
    assert report.series[0].quota == 10
    assert report.series[-1].quota == 20
    assert sum(item.quota for item in report.series) == 30


async def test_dst_repeated_hour_is_not_merged(store):
    start, end = ts("2026-11-01T00:00:00-04:00"), ts("2026-11-02T00:00:00-05:00")
    await store.save_logs(
        [
            LogRecord(id=1, created_at=ts("2026-11-01T01:30:00-04:00"), type=2, quota=11),
            LogRecord(id=2, created_at=ts("2026-11-01T01:30:00-05:00"), type=2, quota=22),
        ],
        end,
        start,
    )
    report = await Analytics(store, "America/New_York").snapshot(
        StatisticsQuery(start=date(2026, 11, 1), end=date(2026, 11, 1))
    )
    assert len(report.series) == 25
    assert report.series[1].quota == 11 and report.series[2].quota == 22
    assert report.series[1].label != report.series[2].label


async def test_empty_snapshot_does_not_claim_complete(store):
    report = await Analytics(store, "UTC").snapshot(
        StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 8))
    )
    assert report.summary.quota == 0 and report.ranking == []
    assert report.coverage.complete is False
    assert len(report.series) == 24


async def test_channel_leaders_are_part_of_the_same_statistics_snapshot(store):
    start, end = ts("2026-11-08T00:00:00+00:00"), ts("2026-11-09T00:00:00+00:00")
    await store.save_logs(
        [LogRecord(id=1, created_at=start, type=2, channel=12, quota=900)], end, start
    )
    snapshot = await Analytics(store, "UTC").snapshot(
        StatisticsQuery(start=date(2026, 11, 8), end=date(2026, 11, 8))
    )
    assert snapshot.channel_leaders[0].key == "12"
    assert snapshot.channel_leaders[0].quota == snapshot.summary.quota
