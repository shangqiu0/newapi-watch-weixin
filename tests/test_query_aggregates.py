import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from newapi_watch.client import NewAPIClient
from newapi_watch.config import Settings
from newapi_watch.periods import StatisticsQuery


def config(tmp_path):
    return Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "q.db",
        newapi_url="https://up.invalid",
        newapi_token="token",
        newapi_user_id=1,
        request_attempts=1,
    )


@pytest.mark.parametrize(
    "dimension,path,key,label",
    [
        ("model", "/api/data/", "model_name", "test-model"),
        ("user", "/api/data/users", "username", "alice"),
        ("channel", "/api/data/flow", "channel_id", 93),
    ],
)
async def test_aggregates_use_real_upstream_grouping_not_raw_logs(
    tmp_path, dimension, path, key, label
):
    from newapi_watch.query_engine.aggregates import AggregateReader

    stamp = int(time.time()) // 3600 * 3600
    day = datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).date()
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        assert request.url.path == path
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        key: label,
                        "created_at": stamp,
                        "quota": 120,
                        "count": 3,
                        "token_used": 9,
                        "key": "private",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        result = await AggregateReader(NewAPIClient(config(tmp_path), http)).snapshot(
            StatisticsQuery(start=day, end=day, dimension=dimension)
        )
    assert result["summary"]["quota"] == 120 and result["ranking"][0]["quota"] == 120
    assert "error_records" in result["unavailable_metrics"] and not result["coverage"]["complete"]
    assert calls == [path] and "private" not in str(result)
    if dimension == "channel":
        assert result["series"] == []


async def test_empty_aggregate_and_missing_counters_are_unknown_not_zero(tmp_path):
    from newapi_watch.query_engine.aggregates import AggregateReader, AggregateUnavailable

    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for data in ([], [{"model_name": "m", "count": 1}]):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, data=data: httpx.Response(200, json={"success": True, "data": data})
            )
        ) as http:
            with pytest.raises(AggregateUnavailable):
                await AggregateReader(NewAPIClient(config(tmp_path), http)).snapshot(
                    StatisticsQuery(start=day, end=day)
                )


async def test_aggregate_permission_and_429_do_not_trigger_raw_log_fallback(tmp_path):
    from newapi_watch.client import UpstreamError
    from newapi_watch.query_engine.aggregates import AggregateReader, AggregateUnavailable

    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for status in (403, 429):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _, status=status: httpx.Response(status))
        ) as http:
            with pytest.raises(UpstreamError) as exc:
                await AggregateReader(NewAPIClient(config(tmp_path), http)).snapshot(
                    StatisticsQuery(start=day, end=day)
                )
            assert not isinstance(exc.value, AggregateUnavailable)
