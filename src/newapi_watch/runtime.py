"""Resource lifetime and isolated periodic jobs; single-process deployment only."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .analytics import Snapshot
from .assistant import Assistant
from .bot import BotGateway
from .client import NewAPIClient
from .commands import Commands
from .config import Settings
from .control_models import LLMConfig
from .demo import DemoTransport
from .health import Health
from .monitor import Monitor
from .monitoring.models import MonitoringConfig
from .monitoring.service import MonitoringService
from .notifications import GroupWebhookNotifier, NotificationWorker
from .periods import StatisticsQuery
from .plugins import Registry
from .query_engine.engine import QueryEngine
from .query_engine.error_search import ErrorSearch
from .reports import Reports
from .retention import RetentionConfig, RetentionService
from .store import Store

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        llm_http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.owns_http = http is None
        transport = httpx.MockTransport(DemoTransport()) if settings.demo else None
        self.http = http or httpx.AsyncClient(
            transport=transport, follow_redirects=False, timeout=settings.request_timeout
        )
        effective = (
            settings.model_copy(update={"newapi_url": "https://demo.invalid"})
            if settings.demo
            else settings
        )
        self.store = Store(settings.database)
        self.client = NewAPIClient(effective, self.http)
        self.monitor = Monitor(settings, self.store, self.client)
        self.reports = Reports(settings, self.store)
        self.commands = Commands(settings, self.store, self.client, self.monitor, self.reports)
        self.query_engine = QueryEngine(self.store, self.client)
        self.error_search = ErrorSearch(self.client)
        self.commands.error_search = self.error_search
        self.owns_llm_http = llm_http is None and settings.llm_enabled and not settings.demo
        self.llm_http = llm_http or (
            httpx.AsyncClient(timeout=25, follow_redirects=False)
            if self.owns_llm_http
            else self.http
        )
        llm = LLMConfig(
            enabled=settings.llm_enabled and (not settings.demo or llm_http is not None),
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
        self.assistant = Assistant(
            "legacy-instance",
            settings.platform_name,
            self.commands,
            llm,
            self.llm_http,
            binding_id="legacy-wecom",
        )
        self.bot = BotGateway(settings, self.store, self.assistant)
        self.commands.health = Health(settings, self.store, lambda: self.bot.authenticated)
        self.registry = Registry()
        # Always use a separate real HTTP client for notifications, never the demo transport.
        self.notify_http = httpx.AsyncClient(timeout=15, follow_redirects=False)
        webhook = (
            GroupWebhookNotifier(settings.group_webhook.get_secret_value(), self.notify_http)
            if settings.group_webhook.get_secret_value() and not settings.demo
            else None
        )
        self.notifications = NotificationWorker(settings, self.store, webhook, self.notify_http)
        self.tasks: list[asyncio.Task[Any]] = []
        self.stop = asyncio.Event()
        self.policies: MonitoringService | None = None
        self.retention_callback: Callable[[], Awaitable[int]] | None = None
        self.upstream: Any = None
        self.commands.upstream = None

    async def open(
        self, *, start_tasks: bool = True, monitoring: MonitoringConfig | None = None
    ) -> None:
        await self.store.open()
        await self.store.bind_instance("DEMO" if self.settings.demo else self.settings.newapi_url)
        self.registry.load(self.settings.plugins, self)
        self.commands.extensions = self.registry.commands
        if monitoring is not None:
            self.client.throttle.configure(
                0 if self.settings.demo else monitoring.requests.min_interval_seconds,
                monitoring.requests.rate_limit_cooldown_seconds,
            )
            self.policies = MonitoringService(self, monitoring)
            await self.policies.initialize()
            last_run = json.loads(await self.store.get_meta("policy:last_run") or "null")
            if last_run and last_run.get("status") == "queued":
                last_run.update(status="interrupted", finished_at=time.time())
                await self.store.set_meta("policy:last_run", json.dumps(last_run))
                await self.store.set_meta("policy:run:" + last_run["id"], json.dumps(last_run))
            self.commands.live_queries = True
            self.commands.query_engine = self.query_engine

            async def live_snapshot(query: StatisticsQuery) -> Snapshot:
                return Snapshot.model_validate(await self.commands.data.statistics(query))

            self.reports.live_snapshot = live_snapshot
        if self.upstream is not None:
            await self.upstream.initialize()
        if not start_tasks:
            return
        # Initial tasks execute independently: one slow endpoint does not prevent others starting.
        jobs: list[tuple[str, float, Callable[[], Awaitable[Any]]]] = [
            ("channels", self.settings.poll_seconds, self.monitor.collect_channels),
            ("logs", self.settings.log_seconds, self.monitor.collect_logs),
            ("notifications", 1, self.notifications.process_one),
            ("events", 1, self.reconcile_event),
            ("report", 30, self.daily),
            ("retention", 60, self.prune),
        ]
        if self.policies is not None:
            jobs = [
                ("policy-checks", 1, self.policies.schedule),
                ("notifications", 1, self.notifications.process_one),
                ("retention", 60, self.prune),
            ]
        if self.policies is not None:
            jobs.append(("interactive-queries", 1, self.query_engine.jobs.tick))
        if self.settings.probe_enabled:
            jobs.append(("probes", self.settings.probe_seconds, self.monitor.probe_enabled))
        jobs.extend(
            (name, job.interval, job.callback) for name, job in self.registry.collectors.items()
        )
        self.tasks = [
            asyncio.create_task(self.periodic(name, interval, job), name=name)
            for name, interval, job in jobs
        ]
        if self.settings.bot_id and not self.settings.demo:
            self.tasks.append(asyncio.create_task(self.periodic("wecom", 5, self.bot.run)))

    async def run_job(self, name: str, job: Callable[[], Awaitable[Any]]) -> bool:
        try:
            await job()
        except Exception as exc:
            logger.error("job_failed name=%s type=%s", name, type(exc).__name__)
            await self.store.condition(
                "service:job:" + name,
                True,
                f"后台任务 {name} 执行失败，请检查服务",
                "warning",
                time.time(),
            )
            return False
        await self.store.set_meta("job:" + name, str(time.time()))
        await self.store.condition(
            "service:job:" + name, False, f"后台任务 {name} 已恢复", "warning", time.time()
        )
        return True

    async def periodic(self, name: str, interval: float, job: Callable[[], Awaitable[Any]]) -> None:
        while not self.stop.is_set():
            await self.run_job(name, job)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def reconcile_event(self) -> None:
        if await self.store.take_event():
            if not await self.monitor.collect_channels():
                await self.store.set_meta("channel_event_pending", "1")

    async def daily(self, now: datetime | None = None) -> None:
        if not self.settings.report_enabled:
            return
        if await self.store.get_meta("logs_last_ok") is None:
            return
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        if now.hour < self.settings.report_hour:
            return
        day = now.date() - timedelta(days=1)
        if await self.store.get_meta("daily_report") == day.isoformat():
            return
        report = await self.reports.daily(day)
        await self.store.queue_report(day.isoformat(), report.text, report.image, now.timestamp())

    async def prune(self) -> None:
        now = time.time()
        next_at = await self.store.get_meta("retention:next_at")
        if not next_at:
            await self.store.set_meta("retention:next_at", str(now + 3600))
            return
        if now < float(next_at):
            return
        config = self.settings.retention or RetentionConfig(
            logs_days=self.settings.retention_days,
            history_days=self.settings.retention_days,
            audits_days=self.settings.retention_days,
        )
        async with self.query_engine.jobs.lock:
            result = await RetentionService(self.store).prune(config, now=now)
        if config.enabled and self.retention_callback:
            count = await self.retention_callback()
            result["deleted"]["operations"] = count
            result["backlog"] = result["backlog"] or count >= 500
            # Central operation records are a separate DB. Include their batch
            # in the same visible run and retry a backlog after 60s, not 1h.
            await self.store.set_meta("retention:last_run", json.dumps(result))
        await self.store.set_meta(
            "retention:next_at", str(time.time() + (60 if result["backlog"] else 3600))
        )

    async def close(self) -> None:
        self.stop.set()
        if self.upstream is not None:
            self.upstream.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.policies:
            await self.policies.close()
        if self.upstream is not None:
            await self.upstream.close()
        await self.query_engine.close()
        await self.error_search.cache.close()
        if self.owns_http:
            await self.http.aclose()
        await self.notify_http.aclose()
        if self.owns_llm_http:
            await self.llm_http.aclose()
        await self.store.close()
