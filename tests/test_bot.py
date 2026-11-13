import httpx
import pytest

from newapi_watch.bot import BotGateway
from newapi_watch.client import NewAPIClient
from newapi_watch.commands import Commands
from newapi_watch.config import Settings
from newapi_watch.models import Channel
from newapi_watch.monitor import Monitor
from newapi_watch.reports import Reports
from newapi_watch.store import Store


@pytest.mark.parametrize("chat_type", ["single", "group"])
async def test_bot_single_and_group_same_commands_and_duplicate_is_dropped(tmp_path, chat_type):
    settings = Settings(_env_file=None, demo=True, newapi_url="https://relay.example")
    store = Store(tmp_path / "db")
    await store.open()
    try:
        await store.sync_channels([Channel(id=1, name="test", status=1)], 100)
        async with httpx.AsyncClient() as http:
            client = NewAPIClient(settings, http)
            commands = Commands(
                settings, store, client, Monitor(settings, store, client), Reports(settings, store)
            )
            gateway = BotGateway(settings, store, commands)
            result = await gateway.handle(
                {
                    "body": {
                        "msgid": "m1",
                        "chattype": chat_type,
                        "from": {"userid": "any"},
                        "text": {"content": "渠道状态"},
                    }
                }
            )
            assert "test" in result.text
            assert (
                await gateway.handle(
                    {
                        "body": {
                            "msgid": "m1",
                            "chattype": chat_type,
                            "text": {"content": "渠道状态"},
                        }
                    }
                )
                is None
            )
    finally:
        await store.close()


async def test_bot_malformed_message_is_rejected(tmp_path):
    settings = Settings(_env_file=None, demo=True)
    store = Store(tmp_path / "db")
    await store.open()
    try:
        gateway = BotGateway(settings, store, None)
        assert await gateway.handle({}) is None
        assert (
            await gateway.handle({"body": {"msgid": "m1", "text": {"content": "x" * 3000}}}) is None
        )
    finally:
        await store.close()


async def test_sdk_stream_reply_reaches_transport_and_failed_send_can_retry(tmp_path):
    from aibot import WSClient, WSClientOptions

    from newapi_watch.bot import SDKLogger
    from newapi_watch.models import CommandResult

    cfg = Settings(_env_file=None, demo=True)
    db = Store(tmp_path / "sdk")
    await db.open()

    class CommandService:
        async def execute(self, text, **context):
            return CommandResult(text="response", image=b"png-bytes")

    class Transport:
        def __init__(self):
            self.messages = []
            self.fail = True

        async def send_reply(self, request_id, body, cmd):
            if self.fail and body.get("stream", {}).get("finish"):
                self.fail = False
                raise RuntimeError("network dropped")
            self.messages.append((request_id, body, cmd))
            return {"errcode": 0}

    try:
        gateway = BotGateway(cfg, db, CommandService())
        sdk = WSClient(WSClientOptions(bot_id="test", secret="test", logger=SDKLogger()))
        transport = Transport()
        sdk._ws_manager = transport
        gateway.client = sdk
        frame = {"headers": {"req_id": "r1"}, "body": {"msgid": "m1", "text": {"content": "日报"}}}
        await gateway._on_text(frame)
        await gateway._on_text(frame)
        assert len([item for item in transport.messages if item[1]["stream"]["finish"]]) == 1
        req_id, body, cmd = transport.messages[-1]
        assert req_id == "r1"
        assert body["stream"]["finish"] is True
        assert body["stream"]["content"] == "response"
        assert body["stream"]["msg_item"][0]["image"]["md5"] == "e8c0e28b42bd2f48ea34ccdc6593f88a"
    finally:
        await db.close()


async def test_bot_inflight_receipt_expires_after_crash_window(tmp_path):
    import time

    from newapi_watch.models import CommandResult

    class Commands:
        async def execute(self, text, **context):
            return CommandResult(text="ok")

    cfg = Settings(_env_file=None, demo=True, command_timeout=5)
    db = Store(tmp_path / "lease")
    await db.open()
    try:
        gateway = BotGateway(cfg, db, Commands())
        await gateway.handle({"body": {"msgid": "crashed", "text": {"content": "帮助"}}})
        assert await db.receipt("bot:crashed", time.time() + 86400, time.time() + 120)
    finally:
        await db.close()
