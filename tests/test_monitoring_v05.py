import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from newapi_watch.config import Settings
from newapi_watch.runtime import Runtime


def settings(tmp_path):
    return Settings(_env_file=None, demo=True, database=tmp_path / "rules.sqlite3")


def test_error_template_only_allows_safe_placeholders():
    from newapi_watch.monitoring.models import ErrorRule

    with pytest.raises(ValidationError):
        ErrorRule(template="{channel.__class__}")
    with pytest.raises(ValidationError):
        ErrorRule(status_codes=[200])
    assert ErrorRule(status_codes=[400, 500]).status_codes == [400, 500]


async def test_error_rule_filters_deduplicates_and_flushes_cooldown_count(tmp_path):
    from newapi_watch.models import LogRecord
    from newapi_watch.monitoring.models import ErrorRule, MonitoringConfig
    from newapi_watch.monitoring.service import MonitoringService

    r = Runtime(settings(tmp_path))
    await r.open(start_tasks=False)
    try:
        cfg = MonitoringConfig(
            errors=ErrorRule(enabled=True, status_codes=[400], cooldown_seconds=60)
        )
        service = MonitoringService(r, cfg)
        log = LogRecord(
            id=1,
            type=5,
            created_at=1000,
            channel=93,
            channel_name="bailian",
            http_status=400,
            error_code="invalid_parameter_error",
            error_message="status_code=400 developer unsupported",
            model_name="kimi",
        )
        await service.errors([log], now=1000)
        await service.errors(
            [
                log,
                log.model_copy(update={"id": 2}),
                log.model_copy(update={"id": 3, "http_status": 500}),
            ],
            now=1001,
        )
        assert len(await r.store.notifications()) == 1
        await service.errors([], now=1060)
        notifications = await r.store.notifications()
        assert len(notifications) == 2 and "1 次" in notifications[1]["content"]
        assert "400" in notifications[0]["content"] and "bailian" in notifications[0]["content"]
        alerts = await r.commands.operations.alerts()
        assert alerts["total"] == 1 and "400" in alerts["items"][0]["title"]
    finally:
        await r.close()


async def test_balance_thresholds_are_per_user_disabled_rules_do_not_query(tmp_path):
    from newapi_watch.monitoring.models import BalanceRule, MonitoringConfig
    from newapi_watch.monitoring.service import MonitoringService

    seen = []

    def upstream(request):
        user = int(request.url.path.split("/")[-1])
        seen.append(user)
        return httpx.Response(
            200, json={"success": True, "data": {"id": user, "username": str(user), "quota": 70}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as h:
        r = Runtime(settings(tmp_path), http=h)
        await r.open(start_tasks=False)
        try:
            cfg = MonitoringConfig(
                balances=[
                    BalanceRule(user_id=1, threshold=100, enabled=True),
                    BalanceRule(user_id=2, threshold=50, enabled=True),
                    BalanceRule(user_id=3, threshold=500, enabled=False),
                ]
            )
            service = MonitoringService(r, cfg)
            await service.balances(now=1000)
            await service.balances(now=1010)
            assert seen == [1, 2] and len(await r.store.notifications()) == 1
            assert "用户 #1" in (await r.store.notifications())[0]["content"]
        finally:
            await r.close()


async def test_report_disabled_is_noop_and_enabled_daily_is_durable_deduplicated(tmp_path):
    from newapi_watch.monitoring.models import MonitoringConfig, ReportSchedule
    from newapi_watch.monitoring.service import MonitoringService

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"success": True, "data": {"items": [], "total": 0}})
        )
    ) as h:
        r = Runtime(settings(tmp_path), http=h)
        await r.open(start_tasks=False)
        try:
            now = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
            await MonitoringService(r, MonitoringConfig()).reports(now)
            assert not await r.store.notifications()
            cfg = MonitoringConfig(daily=ReportSchedule(enabled=True, hour=9, with_image=False))
            await MonitoringService(r, cfg).reports(now)
            await MonitoringService(r, cfg).reports(now)
            assert len(await r.store.notifications()) == 1
            assert "日报" in (await r.store.notifications())[0]["content"]
        finally:
            await r.close()


async def test_webhook_subscription_and_actual_all_mention_do_not_cross_platform(tmp_path):
    from newapi_watch.control_models import BindingInput, PlatformInput
    from newapi_watch.control_store import ControlStore
    from newapi_watch.targets import Targets

    control = ControlStore(tmp_path / "control")
    await control.open()
    try:
        a = await control.create_platform(
            PlatformInput(
                name="A", newapi_url="https://a.invalid", newapi_token="x", newapi_user_id=1
            )
        )
        b = await control.create_platform(
            PlatformInput(
                name="B", newapi_url="https://b.invalid", newapi_token="y", newapi_user_id=1
            )
        )
        for platform, name, events in [
            (a, "a-errors", ["request_error"]),
            (a, "a-daily", ["daily"]),
            (b, "b-errors", ["request_error"]),
        ]:
            await control.create_binding(
                platform["id"],
                BindingInput(
                    name=name,
                    kind="webhook",
                    webhook_url=f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={name}",
                    events=events,
                ),
            )
        sent = []

        def receiver(request):
            sent.append((request.url.params["key"], json.loads(request.content)))
            return httpx.Response(200, json={"errcode": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as http:
            target = Targets(control, a["id"], "A", http)
            await target.send_for(1, "渠道异常告警", kind="request_error", mention_all=True)
            await target.send_for(1, "渠道异常告警", kind="request_error", mention_all=True)
        assert len(sent) == 1 and sent[0][0] == "a-errors"
        assert sent[0][1]["msgtype"] == "text" and sent[0][1]["text"]["mentioned_list"] == ["@all"]
        assert sent[0][1]["text"]["content"] == "平台：A    类型：渠道异常告警"
    finally:
        await control.close()


async def test_balance_lookup_failure_never_creates_zero_balance_alert(tmp_path):
    from newapi_watch.monitoring.models import BalanceRule, MonitoringConfig
    from newapi_watch.monitoring.service import MonitoringService

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as h:
        r = Runtime(settings(tmp_path), http=h)
        await r.open(start_tasks=False)
        try:
            await MonitoringService(
                r, MonitoringConfig(balances=[BalanceRule(user_id=1, threshold=100, enabled=True)])
            ).balances(now=1000)
            assert not await r.store.notifications()
            assert "403" in await r.store.get_meta("balance_error:1")
        finally:
            await r.close()


async def test_weekly_schedule_weekday_gate_and_duplicate_tick(tmp_path):
    from newapi_watch.monitoring.models import MonitoringConfig, ReportSchedule
    from newapi_watch.monitoring.service import MonitoringService

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"success": True, "data": {"items": [], "total": 0}})
        )
    ) as h:
        r = Runtime(settings(tmp_path), http=h)
        await r.open(start_tasks=False)
        try:
            cfg = MonitoringConfig(
                weekly=ReportSchedule(enabled=True, weekday=0, hour=9, with_image=False)
            )
            service = MonitoringService(r, cfg)
            await service.reports(datetime(2026, 11, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
            assert not await r.store.notifications()
            monday = datetime(2026, 11, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
            await service.reports(monday)
            await service.reports(monday)
            records = await r.store.notifications()
            assert (
                len(records) == 1
                and "周报" in records[0]["content"]
                and "11月2日" in records[0]["content"]
            )
        finally:
            await r.close()


async def test_no_subscribed_destination_is_not_reported_as_delivered(tmp_path):
    from newapi_watch.control_models import BindingInput, PlatformInput
    from newapi_watch.control_store import ControlStore
    from newapi_watch.targets import Targets

    control = ControlStore(tmp_path / "control")
    await control.open()
    try:
        p = await control.create_platform(
            PlatformInput(
                name="A", newapi_url="https://a.invalid", newapi_token="x", newapi_user_id=1
            )
        )
        await control.create_binding(
            p["id"],
            BindingInput(
                name="daily only",
                kind="webhook",
                events=["daily"],
                webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            ),
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="订阅"):
                await Targets(control, p["id"], "A", http, dry_run=True).send_for(
                    1, "error", kind="request_error"
                )
    finally:
        await control.close()
