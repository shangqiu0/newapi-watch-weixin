import httpx
import pytest

from newapi_watch.client import NewAPIClient
from newapi_watch.commands import Commands
from newapi_watch.config import Settings
from newapi_watch.models import Channel
from newapi_watch.monitor import Monitor
from newapi_watch.reports import Reports
from newapi_watch.store import Store


@pytest.fixture
async def commands(tmp_path):
    settings = Settings(_env_file=None, demo=True, newapi_url="https://relay.example")
    store = Store(tmp_path / "db")
    await store.open()

    def handler(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": 1,
                            "username": "alice",
                            "quota": 120,
                            "password": "leak",
                            "access_token": "leak-token",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = NewAPIClient(settings, http)
        await store.sync_channels([Channel(id=1, name="渠道一", status=1)], 100)
        yield Commands(
            settings, store, client, Monitor(settings, store, client), Reports(settings, store)
        )
    await store.close()


@pytest.mark.parametrize("actor", ["admin", "ordinary", "unknown", ""])
async def test_all_actors_have_identical_query_permissions(commands, actor):
    result = await commands.execute("用户 alice", actor=actor)
    assert "alice" in result.text
    assert "120" in result.text
    assert "leak" not in result.text


async def test_group_mention_and_command_aliases(commands):
    result = await commands.execute("@机器人 渠道状态")
    assert "渠道一" in result.text
    assert "启用" in result.text
    assert "未采集" not in result.text


async def test_channel_detail_unknown_input_and_probe_gate(commands):
    assert "渠道一" in (await commands.execute("渠道 1 详情")).text
    assert "不存在" in (await commands.execute("渠道 999 详情")).text
    assert "未开启" in (await commands.execute("渠道 1 测试")).text
    assert "帮助" in (await commands.execute("删除全部渠道")).text
    assert "帮助" in (await commands.execute("  ")).text


async def test_report_command_invalid_date_is_user_readable(commands):
    result = await commands.execute("日报 2026-99-99")
    assert "日期" in result.text


async def test_channel_silence_is_shared_not_role_gated(commands):
    assert "静默" in (await commands.execute("告警 静默 渠道1 1h", actor="anyone")).text
    assert "1" in (await commands.execute("渠道 1")).text
