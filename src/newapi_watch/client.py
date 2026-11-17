"""Version-tolerant, bounded NewAPI administrative API adapter."""

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import Channel, LogPage, LogRecord, ProbeResult, UsageCounters, UserInfo
from .redaction import redact_text
from .upstream_throttle import RequestThrottle
from .upstream_throttle import UpstreamError as UpstreamError


class NewAPIClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        *,
        throttle: RequestThrottle | None = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.throttle = throttle or RequestThrottle(min_interval=0 if settings.demo else 1.2)
        self.headers = {
            "Authorization": f"Bearer {settings.newapi_token.get_secret_value()}",
            "New-Api-User": str(settings.newapi_user_id),
        }

    async def request(
        self, path: str, params: Mapping[str, str | int] | None = None, *, retry: bool = True
    ) -> dict[str, Any]:
        attempts = self.settings.request_attempts if retry else 1
        for attempt in range(attempts):
            try:
                async with self.throttle.slot():
                    response = await self.http.get(
                        self.settings.newapi_url + path,
                        params=params,
                        headers=self.headers,
                        timeout=self.settings.request_timeout,
                        follow_redirects=False,
                    )
                    if response.status_code == 429:
                        raise self.throttle.rate_limited(response.headers.get("Retry-After"))
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise UpstreamError("NewAPI 连接失败或超时") from None
            else:
                if response.status_code in {500, 502, 503, 504} and attempt + 1 < attempts:
                    await asyncio.sleep(self.settings.retry_seconds * 2**attempt)
                    continue
                if response.status_code != 200:
                    raise UpstreamError(f"NewAPI HTTP {response.status_code}")
                try:
                    payload = response.json()
                except ValueError:
                    raise UpstreamError("NewAPI 返回非 JSON 数据") from None
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    raise UpstreamError("NewAPI 请求被拒绝，请检查管理令牌或接口版本")
                self.throttle.succeeded()
                return payload
            await asyncio.sleep(self.settings.retry_seconds * 2**attempt)
        raise UpstreamError("NewAPI 请求失败")

    async def pages(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        expected_total: int | None = None
        for page in range(1, self.settings.max_pages + 1):
            query: dict[str, str | int] = dict(params or {})
            query.update(p=page, page_size=self.settings.page_size)
            payload = await self.request(path, query)
            data = payload.get("data")
            total: int | None = None
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and isinstance(data.get("items"), list):
                items = data["items"]
                if isinstance(data.get("total"), int):
                    total = data["total"]
            else:
                raise UpstreamError("NewAPI 列表结构不兼容，保留现有快照")
            if total is not None:
                if total < 0 or (expected_total is not None and total != expected_total):
                    raise UpstreamError("NewAPI 分页期间总数变化，本次检查未知，请稍后重试")
                expected_total = total
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                    raise UpstreamError("NewAPI 列表数据无效")
                if item["id"] in seen:
                    raise UpstreamError("NewAPI 分页重复，请稍后重新采集")
                seen.add(item["id"])
                result.append(item)
            if total is not None and len(result) >= total:
                return result
            if total is not None:
                if not items:
                    raise UpstreamError("NewAPI 分页不完整，未更新本地数据")
                # Upstream may clamp page_size. A short page is not the end when
                # its declared total says there are more records.
                continue
            if not items or len(items) < self.settings.page_size:
                return result
        raise UpstreamError("NewAPI 数据超过分页上限，未更新本地数据")

    async def channels(self) -> list[Channel]:
        records = await self.pages("/api/channel/", {"id_sort": "true"})
        try:
            return [Channel.model_validate(item) for item in records]
        except ValidationError:
            raise UpstreamError("NewAPI 渠道字段不兼容") from None

    async def channel(self, channel_id: int) -> Channel:
        if channel_id <= 0:
            raise ValueError("渠道ID必须大于0")
        payload = await self.request(f"/api/channel/{channel_id}")
        try:
            channel = Channel.model_validate(payload.get("data"))
            if channel.id != channel_id:
                raise ValueError("渠道ID不匹配")
            return channel
        except (ValidationError, ValueError):
            raise UpstreamError("NewAPI 单渠道信息不兼容或ID不匹配") from None

    async def usage_counters(self, start: int, end: int) -> UsageCounters:
        payload = await self.request(
            "/api/log/stat",
            {
                "start_timestamp": start,
                "end_timestamp": end - 1,
                "type": 2,
            },
        )
        try:
            return UsageCounters.model_validate(payload.get("data"))
        except ValidationError:
            raise UpstreamError("NewAPI 汇总接口字段不兼容，不能将缺失额度当作零消耗") from None

    async def query_log_page(
        self,
        start: int,
        end: int,
        *,
        page: int,
        page_size: int,
        log_type: int = 0,
        channel_id: int = 0,
    ) -> LogPage:
        """Read only the requested page, distinct from complete report scans."""
        if start < 0 or end <= start or page < 1 or not 1 <= page_size <= 100:
            raise ValueError("日志查询范围或分页参数无效")
        payload = await self.request(
            "/api/log/",
            {
                "start_timestamp": start,
                "end_timestamp": end - 1,
                "p": page,
                "page_size": page_size,
                "type": log_type,
                "channel": channel_id,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise UpstreamError("平台不支持分页日志查询；未回退为全窗口扫描")
        total, size = data.get("total"), data.get("page_size", page_size)
        if type(total) is not int or total < 0 or type(size) is not int or size != page_size:
            raise UpstreamError("平台日志分页总数或分页大小不兼容")
        if not all(isinstance(item, dict) for item in data["items"]):
            raise UpstreamError("平台日志分页字段无效")
        try:
            items = [self.safe_log(item) for item in data["items"]]
        except (ValidationError, TypeError, ValueError):
            raise UpstreamError("平台日志分页字段无效") from None
        if len(items) > page_size or len({r.id for r in items}) != len(items) or len(items) > total:
            raise UpstreamError("平台日志分页重复或数量无效")
        if any(
            not start <= r.created_at < end
            or (log_type and r.type != log_type)
            or (channel_id and r.channel_id != channel_id)
            for r in items
        ):
            raise UpstreamError("平台日志接口未遵守时间/类型/渠道筛选，本页未作为有效结果返回")
        expected = min(page_size, max(0, total - (page - 1) * page_size))
        if len(items) != expected:
            raise UpstreamError("平台日志分页不完整，未将缺失记录当作零记录")
        return LogPage(items=items, total=total, page_size=page_size)

    async def logs(self, start: int, end: int, *, log_type: int = 0) -> list[LogRecord]:
        records = await self.pages(
            "/api/log/", {"start_timestamp": start, "end_timestamp": end, "type": log_type}
        )
        try:
            return [self.safe_log(item) for item in records]
        except ValidationError:
            raise UpstreamError("NewAPI 日志字段不兼容") from None

    async def log_page(self, start: int, end: int, page: int) -> LogPage:
        if not 1 <= page <= self.settings.max_pages:
            raise UpstreamError("报告日志超过分页上限，未生成不完整报告")
        payload = await self.request(
            "/api/log/",
            {
                "start_timestamp": start,
                "end_timestamp": end,
                "type": 0,
                "p": page,
                "page_size": self.settings.page_size,
            },
        )
        return await self._parse_log_page(payload, page)

    async def log_page_error(self, start: int, end: int, page: int, *, log_type: int = 5) -> LogPage:
        if not 1 <= page <= self.settings.max_pages:
            raise UpstreamError("错误日志超过分页上限，未生成不完整报告")
        payload = await self.request(
            "/api/log/",
            {
                "start_timestamp": start,
                "end_timestamp": end,
                "type": log_type,
                "p": page,
                "page_size": self.settings.page_size,
            },
        )
        return await self._parse_log_page(payload, page)

    async def _parse_log_page(self, payload: dict[str, Any], page: int) -> LogPage:
        data = payload.get("data")
        total = size = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            items = data["items"]
            total, size = data.get("total"), data.get("page_size")
            if total is not None and (type(total) is not int or total < 0):
                raise UpstreamError("报告日志分页总数无效")
            if size is not None and (type(size) is not int or size < 1):
                raise UpstreamError("报告日志分页大小无效")
        else:
            raise UpstreamError("报告日志分页结构不兼容")
        if len(items) > self.settings.page_size:
            raise UpstreamError("报告日志单页超过请求上限")
        try:
            records = [self.safe_log(item) for item in items if isinstance(item, dict)]
            if len(records) != len(items) or len({r.id for r in records}) != len(records):
                raise ValueError("分页记录无效或重复")
            return LogPage(items=records, total=total, page_size=size)
        except (ValidationError, ValueError):
            raise UpstreamError("报告日志字段无效或分页重复") from None

    async def users(self, keyword: str) -> list[UserInfo]:
        records = await self.pages("/api/user/search", {"keyword": keyword})
        try:
            return [UserInfo.model_validate(item) for item in records]
        except ValidationError:
            raise UpstreamError("NewAPI 用户字段不兼容") from None

    async def probe(self, channel_id: int) -> ProbeResult:
        # This GET has side effects in upstream; never retry automatically.
        payload = await self.request(f"/api/channel/test/{channel_id}", retry=False)
        return ProbeResult(success=True, time=float(payload.get("time", 0)))

    async def user(self, user_id: int) -> UserInfo:
        if user_id <= 0:
            raise ValueError("用户ID必须大于0")
        payload = await self.request(f"/api/user/{user_id}")
        try:
            if not isinstance(payload.get("data"), dict) or "quota" not in payload["data"]:
                raise ValueError("未提供用户余额")
            user = UserInfo.model_validate(payload["data"])
            if user.id != user_id:
                raise ValueError("用户ID不匹配")
            return user
        except (ValidationError, KeyError, ValueError):
            raise UpstreamError("NewAPI 用户信息不兼容") from None

    def safe_log(self, item: dict[str, Any]) -> LogRecord:
        value = dict(item)
        for field in ("request_id", "upstream_request_id"):
            value[field] = self._trace_id(item.get(field))
        if item.get("type") == 5:
            other = item.get("other", {})
            try:
                other = json.loads(other) if isinstance(other, str) else other
            except (ValueError, TypeError):
                other = {}
            other = other if isinstance(other, dict) else {}
            for field in ("request_id", "upstream_request_id"):
                value[field] = value[field] or self._trace_id(other.get(field))
            content = str(item.get("content", ""))[:8000]
            try:
                parsed = json.loads(content)
            except ValueError:
                parsed = {}
            error = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
            error = error if isinstance(error, dict) else {}
            status = other.get("status_code", error.get("status_code", item.get("status_code", 0)))
            match = re.search(r"(?:status_code|status)[=: ]+(\d{3})", content)
            status = int(status) if str(status).isdigit() else 0
            if not status and match:
                status = int(match[1])
            code = str(error.get("code", other.get("error_code", "")))[:256]
            message = str(error.get("message", content if status else ""))
            # Keep only sanitized error fields; raw other, IP and keys are dropped.
            message = re.sub(r"(?:sk-[A-Za-z0-9_-]+|Bearer\s+\S+)", "[REDACTED]", message)
            message = re.sub(
                r"(?i)(api[_-]?key|token|secret|authorization)\s*[=:]\s*[^\s,;]+",
                r"\1=[REDACTED]",
                message,
            )
            value.update(
                http_status=status if 400 <= status <= 599 else 0,
                error_code=code,
                error_message=redact_text(message, self.settings)[:2000],
            )
        return LogRecord.model_validate(value)

    def _trace_id(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if len(value) > 128 or any(not 33 <= ord(char) <= 126 for char in value):
            return ""
        return redact_text(value, self.settings)
