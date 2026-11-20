"""Trace metadata must survive parsing, cooldown aggregation and Webhook limits."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from newapi_watch.config import Settings
from newapi_watch.monitoring.models import ErrorRule, MonitoringConfig
from newapi_watch.monitoring.service import MonitoringService, preview
from newapi_watch.notifications import GroupWebhookNotifier
from newapi_watch.runtime import Runtime

LOG_AT = 1794627249  # 2026-11-14 11:34:09, Asia/Shanghai
ALERT_AT = 1794627600  # 2026-11-14 11:40:00, Asia/Shanghai
LEGACY_TEMPLATE = "渠道异常告警\n渠道：{channel_name}（#{channel_id}）\n错误码：{error_code}\n错误信息：{error_message}\n时间：{time}（北京时间）"


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    app = Runtime(
        Settings(
            _env_file=None,
            demo=True,
            database=tmp_path / "trace-alert.sqlite3",
            newapi_token="configured-trace-secret",
            report_enabled=False,
        )
    )
    await app.open(start_tasks=False)
    try:
        yield app
    finally:
        await app.close()


def source_log(**values: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "type": 5,
        "created_at": LOG_AT,
        "channel": 49,
        "channel_name": "from 灵芒",
        "model_name": "kimi-k3.1-pro",
        "content": json.dumps(
            {"error": {"code": "502003", "message": "Upstream service is unreachable"}}
        ),
        "other": {"status_code": 502},
        **values,
    }


def service(runtime: Runtime, **values: Any) -> MonitoringService:
    rule = ErrorRule(enabled=True, status_codes=[502], cooldown_seconds=60, **values)
    return MonitoringService(runtime, MonitoringConfig(errors=rule))


@pytest.mark.parametrize("nested", [False, True])
def test_parser_preserves_platform_and_upstream_ids_separately(runtime: Runtime, nested: bool):
    ids = {"request_id": "platform-001", "upstream_request_id": "upstream-002"}
    raw = source_log(**({"other": json.dumps({"status_code": 502, **ids})} if nested else ids))
    parsed = runtime.client.safe_log(raw).model_dump()
    assert parsed.get("request_id") == "platform-001"
    assert parsed.get("upstream_request_id") == "upstream-002"
    assert parsed["created_at"] == LOG_AT
    assert "other" not in parsed and "content" not in parsed


@pytest.mark.parametrize("bad_id", [None, 123, {}, "x" * 129, "id\n伪造：another", "id\x00"])
def test_malformed_optional_trace_ids_do_not_break_log_parsing(runtime: Runtime, bad_id: Any):
    parsed = runtime.client.safe_log(
        source_log(request_id=bad_id, upstream_request_id=bad_id)
    ).model_dump()
    assert parsed.get("request_id") == ""
    assert parsed.get("upstream_request_id") == ""
    assert parsed["http_status"] == 502


@pytest.mark.parametrize(
    "template",
    [
        None,
        "渠道异常告警\n渠道：{channel_name}（#{channel_id}）\n状态码：{status_code}\n"
        "模型：{model}\n错误信息：{error_message}\n错误日志时间：{log_time}\n"
        "上游请求 ID：{upstream_request_id}\n平台请求 ID：{request_id}\n"
        "上轮冷却窗口内同类错误累计：{count} 次",
        "渠道异常告警\n渠道：{channel_name}（#{channel_id}）\n状态码：{status_code}\n"
        "模型：{model}\n错误信息：{error_message}\n"
        "上轮冷却窗口内同类错误累计：{count} 次",
    ],
    ids=["default", "saved-previous-default", "saved-with-implicit-metadata"],
)
async def test_error_notice_uses_log_time_not_alert_time(runtime: Runtime, template: str | None):
    log = runtime.client.safe_log(
        source_log(request_id="platform-001", upstream_request_id="upstream-002")
    )
    options = {} if template is None else {"template": template}
    await service(runtime, **options).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert "错误日志时间：2026年11月14日 11:34:09" in notice
    assert "告警时间：" not in notice
    assert "错误码：" not in notice
    assert "北京时间" not in notice
    assert "日志 ID：" not in notice
    assert notice.splitlines() == [
        "渠道异常告警",
        "状态码：502",
        "渠道：from 灵芒（#49）",
        "模型：kimi-k3.1-pro",
        "错误信息：Upstream service is unreachable",
        "上游请求 ID：upstream-002",
        "平台请求 ID：platform-001",
        "错误日志时间：2026年11月14日 11:34:09",
        "上轮冷却窗口内同类错误累计：1 次",
    ]
    assert "上游请求 ID：platform-001" not in notice


async def test_missing_upstream_id_labels_platform_id_as_fallback(runtime: Runtime):
    log = runtime.client.safe_log(source_log(request_id="platform-only-001"))
    await service(runtime).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert "上游请求 ID：未提供" in notice
    assert "平台请求 ID：platform-only-001" in notice
    assert "上游请求 ID：platform-only-001" not in notice
    assert "错误日志时间：2026年11月14日 11:34:09" in notice


async def test_missing_all_ids_still_includes_real_log_time(runtime: Runtime):
    await service(runtime).errors([runtime.client.safe_log(source_log())], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert "错误日志时间：2026年11月14日 11:34:09" in notice
    assert "上游请求 ID：未提供" in notice
    assert "平台请求 ID：未提供" in notice


async def test_saved_legacy_template_automatically_includes_new_metadata(runtime: Runtime):
    log = runtime.client.safe_log(source_log(upstream_request_id="upstream-legacy-001"))
    await service(runtime, template=LEGACY_TEMPLATE).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert notice.startswith("渠道异常告警\n错误日志时间：")
    assert "2026年11月14日 11:34:09" in notice
    assert "上游请求 ID：upstream-legacy-001" in notice
    assert "时间：2026年11月14日 11:40:00" not in notice
    assert "错误码：" not in notice
    assert "北京时间" not in notice
    assert "from 灵芒（#49）" in notice


async def test_custom_template_can_position_trace_fields_without_duplicates(runtime: Runtime):
    template = "自定义错误\n日志：{log_time}\n上游：{upstream_request_id}\n平台：{request_id}\n{error_message}"
    log = runtime.client.safe_log(
        source_log(request_id="platform-custom-001", upstream_request_id="upstream-custom-002")
    )
    await service(runtime, template=template).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert "日志：2026年11月14日 11:34:09" in notice
    assert notice.count("upstream-custom-002") == 1
    assert notice.count("platform-custom-001") == 1
    assert "错误日志时间：" not in notice and "上游请求 ID：" not in notice


async def test_cooldown_sample_keeps_its_time_and_id_across_database_reopen(runtime: Runtime):
    monitoring = service(runtime)
    first = runtime.client.safe_log(source_log(upstream_request_id="upstream-first"))
    second = runtime.client.safe_log(
        source_log(id=2, created_at=LOG_AT + 1, upstream_request_id="upstream-second")
    )
    await monitoring.errors([first], now=ALERT_AT)
    await monitoring.errors([second], now=ALERT_AT + 1)
    assert len(await runtime.store.notifications()) == 1
    await runtime.store.close()
    await runtime.store.open()
    await service(runtime).errors([], now=ALERT_AT + 60)
    notices = await runtime.store.notifications()
    assert len(notices) == 2
    assert "错误日志时间：2026年11月14日 11:34:10" in notices[1]["content"]
    assert "上游请求 ID：upstream-second" in notices[1]["content"]
    assert "upstream-first" not in notices[1]["content"]
    assert "1 次" in notices[1]["content"]


@pytest.mark.parametrize("created_at", [0, 10**30])
async def test_unusable_log_time_does_not_become_alert_time(runtime: Runtime, created_at: int):
    log = runtime.client.safe_log(source_log(created_at=created_at))
    await service(runtime).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert "错误日志时间：未提供" in notice
    assert "错误日志时间：2026年11月14日 11:40:00" not in notice


def test_legacy_pending_sample_does_not_fabricate_trace_metadata():
    text = preview(ErrorRule(template=LEGACY_TEMPLATE), {"time": "2026年11月14日 11:40:00"})
    assert "错误日志时间：未提供" in text
    assert "上游请求 ID：未提供" in text
    assert "错误日志时间：2026年11月14日 11:40:00" not in text


async def test_trace_fields_redact_configured_secrets(runtime: Runtime):
    log = runtime.client.safe_log(
        source_log(
            request_id="configured-trace-secret", upstream_request_id="configured-trace-secret"
        )
    )
    await service(runtime).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert "上游请求 ID：[REDACTED]" in notice
    assert "configured-trace-secret" not in notice


@pytest.mark.parametrize("message_format", ["text", "markdown"])
async def test_long_legacy_error_reaches_webhook_with_trace_fields(
    runtime: Runtime, message_format: str
):
    log = runtime.client.safe_log(
        source_log(
            content="status_code=502 " + "上游故障详细信息" * 400, upstream_request_id="up-long"
        )
    )
    await service(runtime, template=LEGACY_TEMPLATE).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    received = []

    def webhook(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(webhook)) as http:
        await GroupWebhookNotifier(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-only",
            http,
            message_format=message_format,
        ).send(notice)
    content = received[0][message_format]["content"]
    assert "错误日志时间：2026年11月14日 11:34:09" in content
    assert "上游请求 ID：up-long" in content
    assert "结果已截断" in content


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        ("Asia/Shanghai", "2026年11月14日 11:34:09"),
        ("UTC", "2026年11月14日 03:34:09"),
        ("America/New_York", "2026年11月13日 22:34:09"),
    ],
)
async def test_error_time_matches_current_platform_log_display(
    runtime: Runtime, timezone: str, expected: str
):
    runtime.settings.timezone = timezone
    raw = source_log(id=123456, upstream_request_id="upstream-original")
    log = runtime.client.safe_log(raw)
    await service(runtime).errors([log], now=ALERT_AT)
    notice = (await runtime.store.notifications())[0]["content"]
    assert f"错误日志时间：{expected}\n" in notice
    assert "日志 ID：" not in notice
    assert log.created_at == raw["created_at"] == LOG_AT
    assert "北京时间" not in notice
    assert "告警时间：" not in notice and "错误码：" not in notice


async def test_cooldown_renders_original_timestamp_in_current_platform_timezone(runtime: Runtime):
    monitoring = service(runtime)
    first = runtime.client.safe_log(source_log())
    pending = runtime.client.safe_log(
        source_log(id=2, created_at=LOG_AT + 1, upstream_request_id="upstream-pending")
    )
    await monitoring.errors([first], now=ALERT_AT)
    await monitoring.errors([pending], now=ALERT_AT + 1)
    await runtime.store.close()
    await runtime.store.open()
    runtime.settings.timezone = "UTC"
    await service(runtime).errors([], now=ALERT_AT + 60)
    notice = (await runtime.store.notifications())[1]["content"]
    assert "错误日志时间：2026年11月14日 03:34:10\n" in notice
    assert "日志 ID：" not in notice
    assert "上游请求 ID：upstream-pending" in notice
    assert "11:34:10" not in notice


@pytest.mark.parametrize(
    "template",
    [
        "渠道异常告警\n错误码：{error_code}\n告警时间：{time}（北京时间）\n错误日志时间：{log_time}（北京时间）",
        "渠道异常告警\n错误码：502003\n告警时间：2026年11月14日 11:40:00（北京时间）",
        "错误码：{error_code}\n时间：{time}（北京时间）",
    ],
)
async def test_legacy_templates_do_not_restore_removed_columns(runtime: Runtime, template: str):
    await service(runtime, template=template).errors(
        [runtime.client.safe_log(source_log())], now=ALERT_AT
    )
    notice = (await runtime.store.notifications())[0]["content"]
    assert "错误码：" not in notice
    assert "告警时间：" not in notice
    assert "时间：2026年11月14日 11:40:00" not in notice
    assert "北京时间" not in notice
    assert "错误日志时间：2026年11月14日 11:34:09" in notice


async def test_source_log_id_disambiguates_errors_in_same_second(runtime: Runtime):
    logs = [
        runtime.client.safe_log(source_log(id=101, channel=49)),
        runtime.client.safe_log(source_log(id=102, channel=50)),
    ]
    await service(runtime).errors(logs, now=ALERT_AT)
    notices = [item["content"] for item in await runtime.store.notifications()]
    assert len(notices) == 2
    first = next(text for text in notices if "（#49）" in text)
    second = next(text for text in notices if "（#50）" in text)
    assert "日志 ID：" not in first
    assert "日志 ID：" not in second
    assert all("错误日志时间：2026年11月14日 11:34:09" in text for text in notices)
