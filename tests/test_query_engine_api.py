import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from newapi_watch.auth import Auth
from newapi_watch.console_api import create_console
from newapi_watch.control_models import PlatformInput
from newapi_watch.control_store import ControlStore
from newapi_watch.fleet import Fleet


@pytest.fixture
async def api(tmp_path):
    control = ControlStore(tmp_path / "control")
    await control.open()
    auth = Auth(control, secure_cookie=False)
    await auth.provision("op", "test-password-123456")
    fleet = Fleet(control, demo=True)
    await fleet.open(start_tasks=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console(control, fleet, auth)),
        base_url="http://local",
    ) as http:
        try:
            yield http, control, fleet
        finally:
            await fleet.close()
            await control.close()


async def ready(api, name):
    http, control, fleet = api
    response = await http.post(
        "/console/api/login", json={"username": "op", "password": "test-password-123456"}
    )
    http.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
    p = await control.create_platform(
        PlatformInput(
            name=name,
            newapi_url="https://" + name + ".invalid",
            newapi_token="secret-" + name,
            newapi_user_id=1,
            enabled=True,
        )
    )
    await fleet.reload(p["id"])
    return p


async def test_task_api_auth_csrf_idempotence_and_platform_isolation(api):
    http, _, fleet = api
    assert (await http.get("/console/api/platforms/no/query-tasks")).status_code == 401
    a = await ready(api, "a")
    b = await ready(api, "b")
    root = "/console/api/platforms/" + a["id"]
    today = str(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    body = {"start": today, "end": today, "dimension": "user"}
    token = http.headers.pop("X-CSRF-Token")
    assert (await http.post(root + "/query-tasks", json=body)).status_code == 403
    http.headers["X-CSRF-Token"] = token
    response = await http.post(root + "/query-tasks", json=body)
    assert response.status_code == 202, response.text
    task = response.json()["data"]
    duplicate = await http.post(root + "/query-tasks", json=body)
    assert duplicate.json()["data"]["id"] == task["id"]
    other = "/console/api/platforms/" + b["id"] + "/query-tasks/" + task["id"]
    assert (await http.get(other)).status_code == 404
    cancel = await http.delete(root + "/query-tasks/" + task["id"])
    assert cancel.json()["data"]["status"] == "cancelled"


async def test_statistics_route_aggregate_and_exact_async_are_distinct(api):
    http, _, fleet = api
    p = await ready(api, "a")
    stamp = int(time.time()) // 3600 * 3600

    def upstream(request):
        assert request.url.path == "/api/data/"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "model_name": "test",
                        "created_at": stamp,
                        "quota": 120,
                        "count": 3,
                        "token_used": 8,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
        fleet.entry(p["id"]).runtime.client.http = upstream_http
        root = "/console/api/platforms/" + p["id"]
        data = (await http.get(root + "/statistics?period=今天")).json()["data"]
        assert data["source"] == "newapi_hourly_aggregate" and data["summary"]["quota"] == 120
        exact = (await http.post(root + "/statistics-queries?period=今天&exact=true")).json()[
            "data"
        ]
        assert exact["pending"] and exact["task"]["status"] == "queued"
        assert "summary" not in exact


async def test_chat_task_id_does_not_expose_checkpoint_or_recipient(api):
    http, _, fleet = api
    p = await ready(api, "a")
    from newapi_watch.llm_tools import Tools
    from newapi_watch.query_engine.jobs import JobInput

    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    runtime = fleet.entry(p["id"]).runtime
    task = await runtime.query_engine.jobs.create(
        JobInput(start=day, end=day), "owner-a", context={"actor": "private-recipient"}
    )
    result = await Tools(runtime.commands, owner="owner-b").call(
        "get_query_task", json.dumps({"task_id": task["id"]})
    )
    assert not result["ok"]
    own = await Tools(runtime.commands, owner="owner-a").call(
        "get_query_task", json.dumps({"task_id": task["id"]})
    )
    assert own["ok"] and "private-recipient" not in str(own)
    assert "seen_ids" not in str(own) and "aggregates" not in str(own)


async def test_completed_task_delivers_only_original_enabled_bot_and_conversation(api):
    import json

    from newapi_watch.bot import BotGateway
    from newapi_watch.control_models import BindingInput
    from newapi_watch.query_engine.jobs import JobInput

    http, control, fleet = api
    p = await ready(api, "a")
    entry = fleet.entry(p["id"])
    binding = await control.create_binding(
        p["id"],
        BindingInput(name="testbot", kind="bot", bot_id="test-only", bot_secret="fake-secret"),
    )
    sent = []

    class SDK:
        async def send_message(self, chatid, body):
            sent.append((chatid, body))
            return {"errcode": 0}

    bot = BotGateway(
        entry.runtime.settings,
        entry.runtime.store,
        None,
        namespace="binding:" + binding["id"] + ":",
    )
    bot.client = SDK()
    bot.authenticated = True
    entry.bots.append(bot)
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for index in range(6):
        web = await entry.runtime.query_engine.jobs.create(
            JobInput(start=day, end=day), "web" + str(index), context={"binding_id": "web"}
        )
        async with entry.runtime.store.transaction() as db:
            await db.execute(
                "UPDATE query_jobs SET status='complete',result=? WHERE id=?",
                (json.dumps({"summary": {"quota": 0}}), web["id"]),
            )
    task = await entry.runtime.query_engine.jobs.create(
        JobInput(start=day, end=day),
        "sender",
        context={
            "binding_id": binding["id"],
            "conversation_id": "group:original-group",
            "actor": "u1",
        },
    )
    result = {
        "summary": {"success_records": 2, "error_records": 1, "quota": 100},
        "records_read": 3,
    }
    async with entry.runtime.store.transaction() as db:
        await db.execute(
            "UPDATE query_jobs SET status='complete',result=? WHERE id=?",
            (json.dumps(result), task["id"]),
        )
    await fleet.deliver_queries(p["id"])
    await fleet.deliver_queries(p["id"])
    assert len(sent) == 1 and sent[0][0] == "original-group"
    assert (await entry.runtime.query_engine.jobs.get(task["id"], "sender"))["notified_at"] > 0


async def test_model_stat_tool_can_enqueue_http_status_exact_query(api):
    from newapi_watch.llm_tools import Tools

    http, _, fleet = api
    p = await ready(api, "a")
    result = await Tools(fleet.entry(p["id"]).runtime.commands, owner="bot-context").call(
        "get_statistics", '{"period":"今天","status_codes":[500],"metric":"errors"}'
    )
    assert result["ok"] and result["pending"]
    assert result["task"]["query"]["status_codes"] == [500]
    assert "summary" not in result


async def test_pending_exact_query_returns_task_id_without_waiting_for_second_model_round(api):
    from newapi_watch.assistant import Assistant
    from newapi_watch.control_models import LLMConfig

    http, _, fleet = api
    p = await ready(api, "a")
    calls = []

    def model(request):
        calls.append(1)
        if len(calls) > 1:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "job",
                                    "type": "function",
                                    "function": {
                                        "name": "get_statistics",
                                        "arguments": '{"period":"今天","exact":true}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as provider:
        bot = Assistant(
            p["id"],
            "test",
            fleet.entry(p["id"]).runtime.commands,
            LLMConfig(
                enabled=True, base_url="https://model.invalid/v1", api_key="fake-key", model="test"
            ),
            provider,
            binding_id="test-bot",
        )
        result = await bot.execute("精确统计今天的用量", actor="a", conversation_id="group:g")
        assert result.ok and "q_" in result.text and "已受理" in result.text
        assert len(calls) == 1


async def test_full_query_queue_has_structured_429_in_http_and_model_tool(api):
    from newapi_watch.llm_tools import Tools
    from newapi_watch.query_engine.jobs import JobInput

    http, _, fleet = api
    p = await ready(api, "a")
    runtime = fleet.entry(p["id"]).runtime
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for index in range(50):
        await runtime.query_engine.jobs.create(JobInput(start=day, end=day), str(index))
    response = await http.post(
        "/console/api/platforms/" + p["id"] + "/query-tasks",
        json={"start": str(day), "end": str(day)},
    )
    assert response.status_code == 429
    result = await Tools(runtime.commands, owner="overflow").call(
        "get_statistics", '{"period":"今天","exact":true}'
    )
    assert not result["ok"] and "50" in result["error"]


async def test_get_statistics_can_plan_but_does_not_create_background_tasks(api):
    http, _, fleet = api
    p = await ready(api, "a")
    root = "/console/api/platforms/" + p["id"]
    planned = await http.get(root + "/statistics?period=今天&exact=true")
    assert planned.status_code == 200
    assert planned.json()["data"]["requires_task"] is True
    tasks = (await http.get(root + "/query-tasks")).json()["data"]
    assert tasks["total"] == 0
    submitted = await http.post(root + "/statistics-queries?period=今天&exact=true")
    assert submitted.status_code == 200 and submitted.json()["data"]["pending"]
