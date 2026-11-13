import httpx
import pytest

from newapi_watch.client import NewAPIClient, UpstreamError
from newapi_watch.config import Settings


def settings(**kwargs):
    return Settings(
        _env_file=None,
        demo=True,
        newapi_url="https://relay.example",
        newapi_token="sensitive",
        newapi_user_id=7,
        page_size=2,
        request_attempts=2,
        retry_seconds=0,
        **kwargs,
    )


async def test_channel_pagination_keeps_disabled_and_strips_secrets():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer sensitive"
        assert request.headers["New-Api-User"] == "7"
        page = int(request.url.params["p"])
        items = (
            [
                {"id": 1, "name": "a", "status": 1, "key": "never-store"},
                {"id": 2, "name": "b", "status": 3},
            ]
            if page == 1
            else [{"id": 3, "name": "c", "status": 2}]
        )
        return httpx.Response(200, json={"success": True, "data": {"items": items, "total": 3}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await NewAPIClient(settings(), http).channels()
    assert [item.id for item in result] == [1, 2, 3]
    assert "never-store" not in str(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "message": "secret-value"},
        {"success": True, "data": {}},
        {"success": True, "data": {"items": [{"name": "no-id"}]}},
    ],
)
async def test_malformed_or_business_failure_does_not_return_empty(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(UpstreamError) as exc:
            await NewAPIClient(settings(), http).channels()
        assert "secret-value" not in str(exc.value)


async def test_transient_failure_retries_and_auth_failure_does_not():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, json={"success": True, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await NewAPIClient(settings(), http).channels() == []
    assert calls == 2
    calls = 0

    def auth(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="credential-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(auth)) as http:
        with pytest.raises(UpstreamError):
            await NewAPIClient(settings(), http).channels()
    assert calls == 1


async def test_repeated_pagination_and_redirects_fail_closed():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"items": [{"id": 1, "status": 1}, {"id": 2, "status": 3}], "total": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(UpstreamError):
            await NewAPIClient(settings(), http).channels()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "https://evil.example"})
        )
    ) as http:
        with pytest.raises(UpstreamError):
            await NewAPIClient(settings(), http).channels()


async def test_logs_use_frozen_window_and_actual_channel_field():
    def handler(request):
        assert request.url.params["start_timestamp"] == "100"
        assert request.url.params["end_timestamp"] == "200"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": 9,
                            "created_at": 150,
                            "type": 5,
                            "channel": 12,
                            "other": '{"error_code":"rate_limit"}',
                            "content": "secret",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        logs = await NewAPIClient(settings(), http).logs(100, 200)
    assert logs[0].channel_id == 12
    assert logs[0].type == 5
    assert "secret" not in logs[0].model_dump_json()


async def test_channel_pagination_rejects_changing_total_instead_of_reporting_missing():
    def upstream(request):
        page = int(request.url.params["p"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [{"id": 1, "status": 1}, {"id": 2, "status": 1}]
                    if page == 1
                    else [{"id": 3, "status": 1}],
                    "total": 4 if page == 1 else 3,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        with pytest.raises(UpstreamError, match="分页"):
            await NewAPIClient(settings(), http).channels()
