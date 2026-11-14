import time

import httpx
import pytest

from newapi_watch.client import NewAPIClient
from newapi_watch.config import Settings
from newapi_watch.query_engine.error_search import ErrorSearch


async def test_status_search_only_reads_error_pages_and_cursor_is_owner_bound(tmp_path):
    stamp = int(time.time()) - 1
    seen = []

    def upstream(request):
        seen.append(dict(request.url.params))
        page = int(request.url.params["p"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": (page - 1) * 100 + i,
                            "type": 5,
                            "channel": 93,
                            "created_at": stamp,
                            "other": {"status_code": 500 if i == 1 else 400},
                        }
                        for i in range(1, 101)
                    ],
                    "total": 200,
                    "page_size": 100,
                },
            },
        )

    conf = Settings(
        _env_file=None,
        demo=True,
        newapi_url="https://up.invalid",
        newapi_token="secret",
        newapi_user_id=1,
        request_attempts=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        search = ErrorSearch(NewAPIClient(conf, http))
        first = await search.search(stamp - 100, stamp + 1, owner="a", status=500, channel=93)
        assert len(first["items"]) == 1 and first["total"] is None and first["has_more"]
        assert seen[0]["type"] == "5" and seen[0]["channel"] == "93"
        with pytest.raises(ValueError):
            await search.search(0, 1, owner="b", status=500, cursor=first["cursor"])
        next_batch = await search.search(0, 1, owner="a", status=500, cursor=first["cursor"])
        assert next_batch["items"][0]["id"] == 101 and not next_batch["has_more"]
        await search.cache.close()


async def test_scan_budget_preserves_successful_page_as_continuation(tmp_path):
    stamp = int(time.time()) - 1

    def upstream(request):
        if request.url.params["p"] == "2":
            raise TimeoutError("controlled budget")
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {"id": i, "created_at": stamp, "type": 5, "other": {"status_code": 400}}
                        for i in range(1, 101)
                    ],
                    "total": 300,
                    "page_size": 100,
                },
            },
        )

    conf = Settings(
        _env_file=None,
        demo=True,
        newapi_url="https://up.invalid",
        newapi_token="secret",
        newapi_user_id=1,
        request_attempts=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        search = ErrorSearch(NewAPIClient(conf, http))
        result = await search.search(stamp - 100, stamp + 1, owner="a", status=500)
        assert result["has_more"] and result["cursor"] and result["scanned_records"] == 100
        assert not result["complete"] and result["budget_exhausted"]


async def test_continuation_rejects_same_ids_returned_on_different_pages(tmp_path):
    from newapi_watch.client import UpstreamError

    stamp = int(time.time()) - 1

    def upstream(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {"id": i, "created_at": stamp, "type": 5, "other": {"status_code": 500}}
                        for i in range(1, 101)
                    ],
                    "total": 200,
                    "page_size": 100,
                },
            },
        )

    conf = Settings(
        _env_file=None,
        demo=True,
        newapi_url="https://up.invalid",
        newapi_token="secret",
        newapi_user_id=1,
        request_attempts=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        search = ErrorSearch(NewAPIClient(conf, http))
        first = await search.search(stamp - 100, stamp + 1, owner="a", status=500)
        with pytest.raises(UpstreamError, match="重复"):
            await search.search(0, 1, owner="a", status=500, cursor=first["cursor"])
