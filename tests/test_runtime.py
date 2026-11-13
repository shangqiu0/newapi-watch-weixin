import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from newapi_watch.config import Settings
from newapi_watch.demo import DemoTransport
from newapi_watch.models import CommandResult
from newapi_watch.plugins import Registry
from newapi_watch.runtime import Runtime


async def test_full_demo_collect_fault_recovery_query_and_delivery(tmp_path):
    settings = Settings(
        _env_file=None,
        demo=True,
        database=tmp_path / "db",
        newapi_url="https://demo.invalid",
        api_key="k" * 32,
    )
    transport = DemoTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = Runtime(settings, http=http)
        await runtime.open(start_tasks=False)
        try:
            assert await runtime.monitor.collect_channels()
            assert await runtime.monitor.collect_logs()
            assert "DEMO" in (await runtime.commands.execute("统计")).text
            transport.channel_status = 3
            await runtime.monitor.collect_channels()
            assert any(a["state"] == "firing" for a in await runtime.store.alerts())
            transport.channel_status = 1
            await runtime.monitor.collect_channels()
            assert all(a["state"] == "resolved" for a in await runtime.store.alerts())
            tick = time.time() + 100
            while await runtime.notifications.process_one(now=tick):
                tick += 5
            assert all(item["status"] == "sent" for item in await runtime.store.notifications())
        finally:
            await runtime.close()


async def test_daily_schedule_enqueues_text_image_only_once(tmp_path):
    settings = Settings(_env_file=None, demo=True, database=tmp_path / "db", report_hour=9)
    runtime = Runtime(settings)
    await runtime.open(start_tasks=False)
    try:
        now = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        await runtime.monitor.collect_logs(now=now.timestamp())
        await runtime.daily(now)
        await runtime.daily(now)
        assert len(await runtime.store.notifications()) == 2
        assert await runtime.store.get_meta("daily_report") == "2026-11-09"
    finally:
        await runtime.close()


async def test_job_failure_isolated_and_recorded(tmp_path):
    runtime = Runtime(Settings(_env_file=None, demo=True, database=tmp_path / "db"))
    await runtime.open(start_tasks=False)
    try:

        async def failing():
            raise ValueError("secret-never-expose")

        assert await runtime.run_job("test", failing) is False
        assert "secret-never-expose" not in str(await runtime.store.alerts())
        assert (await runtime.store.alerts())[0]["key"] == "service:job:test"
    finally:
        await runtime.close()


def test_extension_registry_rejects_collisions():
    registry = Registry()

    async def command(text):
        return CommandResult(text="extension")

    registry.command("custom", command)
    with pytest.raises(ValueError):
        registry.command("custom", command)


async def test_periodic_tasks_start_and_shutdown_without_pending_resources(tmp_path):
    import asyncio

    cfg = Settings(_env_file=None, demo=True, database=tmp_path / "periodic", report_enabled=False)
    runtime = Runtime(cfg)
    await runtime.open()
    reached = asyncio.Event()

    async def completed():
        await runtime.monitor.collect_channels()
        await runtime.monitor.collect_logs()
        reached.set()

    task = asyncio.create_task(runtime.periodic("sentinel", 60, completed))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        assert len(await runtime.store.channels()) == 2
    finally:
        runtime.stop.set()
        await task
        await runtime.close()
    assert all(item.done() for item in runtime.tasks)


async def test_daily_waits_for_first_log_collection_instead_of_racing_startup(tmp_path):
    cfg = Settings(_env_file=None, demo=True, database=tmp_path / "startup", report_hour=0)
    runtime = Runtime(cfg)
    await runtime.open(start_tasks=False)
    try:
        await runtime.daily()
        assert await runtime.store.notifications() == []
        await runtime.monitor.collect_logs()
        await runtime.daily()
        assert len(await runtime.store.notifications()) == 2
    finally:
        await runtime.close()
