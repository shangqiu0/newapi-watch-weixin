import pytest
from aibot import WSClient, WSClientOptions

from newapi_watch.bot import SDKLogger
from newapi_watch.config import Settings
from newapi_watch.runtime import Runtime


@pytest.mark.parametrize("chat_type", ["single", "group"])
async def test_real_sdk_offline_frames_query_chart_and_audit_source(tmp_path, chat_type):
    settings = Settings(
        _env_file=None, demo=True, database=tmp_path / "bot.sqlite3", report_enabled=False
    )
    runtime = Runtime(settings)
    await runtime.open(start_tasks=False)

    class Transport:
        def __init__(self):
            self.sent = []

        async def send_reply(self, req_id, body, cmd):
            self.sent.append((req_id, body, cmd))
            return {"errcode": 0}

    try:
        await runtime.monitor.collect_channels()
        await runtime.monitor.collect_logs()
        sdk = WSClient(WSClientOptions(bot_id="offline", secret="offline", logger=SDKLogger()))
        transport = Transport()
        sdk._ws_manager = transport
        runtime.bot.client = sdk
        frame = {
            "headers": {"req_id": "offline-request"},
            "body": {
                "msgid": "message-1",
                "chattype": chat_type,
                "from": {"userid": "same-permission-person"},
                "text": {"content": "图表 模型 近7天"},
            },
        }
        await runtime.bot._on_text(frame)
        await runtime.bot._on_text(frame)
        assert len(transport.sent) == 2
        reply = transport.sent[-1][1]["stream"]
        assert reply["finish"] is True and "DEMO" in reply["content"]
        assert reply["msg_item"][0]["image"]["base64"].startswith("iVBORw0KGgo")
        audits = (await runtime.commands.audits.list())["items"]
        assert len(audits) == 1
        assert audits[0]["source"] == "wecom"
        assert audits[0]["actor_hash"] and "same-permission-person" not in str(audits)
    finally:
        await runtime.close()
