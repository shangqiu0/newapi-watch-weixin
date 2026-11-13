import asyncio
import json

import httpx
import pytest

from newapi_watch.client import NewAPIClient
from newapi_watch.commands import Commands
from newapi_watch.config import Settings
from newapi_watch.models import Channel, CommandResult
from newapi_watch.monitor import Monitor
from newapi_watch.reports import Reports
from newapi_watch.store import Store


@pytest.fixture
async def commands(tmp_path):
    cfg = Settings(
        _env_file=None,
        demo=True,
        timezone="UTC",
        newapi_url="https://relay.example",
        api_key="secret-key-for-tests-only-1234",
    )
    store = Store(tmp_path / "commands.sqlite3")
    await store.open()
    await store.sync_channels([Channel(id=1, name="x", status=1)], 100)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(401))
    ) as http:
        client = NewAPIClient(cfg, http)
        try:
            yield Commands(cfg, store, client, Monitor(cfg, store, client), Reports(cfg, store))
        finally:
            await store.close()


@pytest.mark.parametrize(
    "text",
    [
        "统计 近7天",
        "排行 模型 近7天",
        "排行 用户 昨天 按调用",
        "图表 渠道 近7天",
        "图表 近7天",
        "统计 2026-11-01~2026-11-03",
    ],
)
async def test_extended_reporting_commands_are_not_help_fallback(commands, text):
    result = await commands.execute(text)
    assert "帮助" not in result.text
    assert "quota" in result.text or "消耗" in result.text
    if text.startswith("图表"):
        assert result.image.startswith(b"\x89PNG\r\n\x1a\n")


async def test_ack_silence_and_dead_letter_commands_have_shared_permissions(commands):
    await commands.store.condition("x", True, "test alert", "warning", 101)
    alert_id = (await commands.store.alerts())[0]["id"]
    for actor in ("operator", "ordinary", "unknown"):
        response = await commands.execute("告警 确认 " + alert_id, actor=actor)
        assert "确认" in response.text and response.ok
    assert alert_id in (await commands.execute("告警 列表")).text
    assert "已确认" in (await commands.execute("告警 列表")).text
    assert "静默" in (await commands.execute("告警 静默列表")).text
    await commands.store.finish_notification(1, "dead", 8, 100, "failed", 102)
    assert "1" in (await commands.execute("通知 死信")).text
    assert "重新入队" in (await commands.execute("通知 重试 1", request_id="test-request-001")).text
    duplicate = await commands.execute("通知 重试 1", request_id="test-request-001")
    assert "重复" in duplicate.text


async def test_audit_never_stores_actor_input_or_secret_and_tracks_failures(commands):
    await commands.execute(
        "用户 private-person secret-key-for-tests-only-1234", actor="private-sender", source="wecom"
    )
    await commands.execute("统计 近99天", source="http")
    page = await commands.audits.list()
    assert page["total"] == 2
    assert {item["outcome"] for item in page["items"]} == {"upstream_error", "invalid_input"}
    serialized = json.dumps(page)
    assert "private-person" not in serialized and "private-sender" not in serialized
    assert "secret-key-for-tests-only-1234" not in serialized
    assert all(item["source"] in {"wecom", "http"} for item in page["items"])
    assert any(item["actor_hash"] for item in page["items"])


async def test_cancelled_command_is_audited_not_success(commands):
    started = asyncio.Event()

    async def slow(text):
        started.set()
        await asyncio.Event().wait()
        return CommandResult(text="unreachable")

    commands.extensions["slow"] = slow
    task = asyncio.create_task(commands.execute("slow", actor="someone"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await commands.audits.list())["items"][0]["outcome"] == "cancelled"


async def test_unknown_command_is_not_audited_as_success(commands):
    result = await commands.execute("random-nonexistent-command")
    assert result.ok is False
    assert (await commands.audits.list())["items"][0]["outcome"] == "invalid_input"


@pytest.mark.parametrize("text", ["渠道 999 详情", "渠道 1 测试", "告警 静默 invalid"])
async def test_rejected_builtin_command_is_audited_as_failure(commands, text):
    result = await commands.execute(text)
    assert result.ok is False
    assert (await commands.audits.list())["items"][0]["outcome"] != "success"
