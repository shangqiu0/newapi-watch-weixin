"""Platform-local atomic query ownership, queue state and retention."""

import asyncio
import hashlib
import json
import time
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiosqlite

from ..client import NewAPIClient
from ..persistence import row, rows
from ..store import Store
from .cache import QueryOptions
from .schemas import JobInput


class TaskRepository:
    def __init__(
        self,
        store: Store,
        client: NewAPIClient,
        timezone: str,
        *,
        options: QueryOptions | None = None,
        page_size: int | None = None,
    ) -> None:
        self.store, self.client, self.timezone = store, client, timezone
        self.options = options or QueryOptions.model_construct()
        self.page_size = page_size or client.settings.page_size
        self.lock = asyncio.Lock()
        self.parallel_guard: Any = None
        self.scope = hashlib.sha256(
            (
                client.settings.newapi_url
                + str(client.settings.newapi_user_id)
                + client.settings.newapi_token.get_secret_value()
            ).encode()
        ).hexdigest()

    @staticmethod
    def owner_token(owner: str) -> str:
        return hashlib.sha256(owner.encode()).hexdigest()

    async def create(
        self,
        request: JobInput,
        owner: str,
        *,
        context: dict[str, str] | None = None,
        key_suffix: str = "",
    ) -> dict[str, Any]:
        query = request.query()
        query.reject_future(datetime.now(ZoneInfo(self.timezone)).date())
        owner_hash = self.owner_token(owner)
        dedupe = hashlib.sha256(
            (
                query.model_dump_json()
                + json.dumps(request.status_codes)
                + key_suffix
                + owner_hash
                + self.scope
            ).encode()
        ).hexdigest()
        identifier = "q_" + uuid4().hex
        now = time.time()
        start, end = query.window(self.timezone)
        frozen_end = min(end, int(now) + 1)
        progress = {
            "windows": [[start, frozen_end]],
            "window_index": 0,
            "page": 1,
            "expected_total": None,
            "aggregates": {},
            "seen_ids": [],
            "records_read": 0,
            "total_requests": 0,
            "restarts": 0,
            "committed": {},
            "window_records": 0,
            "request_ceiling": self.options.task_max_requests,
        }
        async with self.store.transaction() as db:
            existing = await row(
                db,
                "SELECT id,query,context_json,status,progress,result,error,next_at,notified_at "
                "FROM query_jobs WHERE dedupe_key=? AND owner_hash=?",
                (dedupe, owner_hash),
            )
            if existing and (
                existing["status"] in {"queued", "running", "waiting", "paused"}
                or (existing["status"] == "complete" and now - existing["next_at"] < 30)
            ):
                return self.public(existing)
            if existing:
                await db.execute(
                    "UPDATE query_jobs SET dedupe_key=? WHERE id=?",
                    (dedupe + ":" + existing["id"], existing["id"]),
                )
            pending = await row(
                db,
                "SELECT COUNT(*) AS n FROM query_jobs WHERE status IN ('queued','running','waiting','paused')",
            )
            if pending and pending["n"] >= 50:
                from ..operations import OperationError

                raise OperationError(429, "当前平台查询任务已达50个，请等待完成或取消旧任务")
            try:
                await db.execute(
                    "INSERT INTO query_jobs(id,dedupe_key,owner_hash,query,context_json,status,progress,next_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                    (
                        identifier,
                        dedupe,
                        owner_hash,
                        json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
                        json.dumps({**(context or {}), "_scope": self.scope}, ensure_ascii=False),
                        json.dumps(progress),
                        now,
                        now,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError:
                existing = await row(
                    db,
                    "SELECT id,query,context_json,status,progress,result,error,next_at,notified_at "
                    "FROM query_jobs WHERE dedupe_key=? AND owner_hash=?",
                    (dedupe, owner_hash),
                )
                if existing:
                    return self.public(existing)
                raise
        return await self.get(identifier, owner)

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        state = (
            json.loads(item.get("progress") or "{}")
            if isinstance(item.get("progress"), str)
            else item.get("progress", {})
        )
        windows = state.get("windows", [])
        return {
            "id": item["id"],
            "query": json.loads(item["query"]) if isinstance(item["query"], str) else item["query"],
            "status": item["status"],
            "progress": {
                **{
                    key: state.get(key, 0)
                    for key in (
                        "records_read",
                        "total_requests",
                        "restarts",
                        "page",
                        "window_index",
                        "window_records",
                    )
                },
                "windows_total": len(windows),
                "from_timestamp": windows[0][0] if windows else None,
                "through_timestamp": windows[-1][1] if windows else None,
            },
            "result": json.loads(item["result"]) if item.get("result") else None,
            "error": item.get("error", ""),
            "next_at": item.get("next_at", 0),
            "notified_at": item.get("notified_at", 0),
        }

    async def admin_get(self, identifier: str) -> dict[str, Any]:
        async with self.store.transaction() as db:
            item = await row(
                db,
                "SELECT id,query,status,progress,result,error,next_at,notified_at FROM query_jobs WHERE id=?",
                (identifier,),
            )
        if not item:
            raise ValueError("查询任务不存在")
        return self.public(item)

    async def admin_page(self, page: int, size: int) -> dict[str, Any]:
        async with self.store.transaction() as db:
            count = await row(db, "SELECT COUNT(*) AS n FROM query_jobs")
            items = await rows(
                db,
                "SELECT id,query,status,progress,result,error,next_at,notified_at FROM query_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            )
        return {
            "items": [self.public(item) for item in items],
            "total": count["n"] if count else 0,
            "page": page,
            "page_size": size,
        }

    async def admin_action(self, identifier: str, action: str) -> dict[str, Any]:
        async with self.store.transaction() as db:
            raw = await row(db, "SELECT progress,status FROM query_jobs WHERE id=?", (identifier,))
            if not raw:
                from ..control_store import ControlError

                raise ControlError(404, "查询任务不存在")
            if action == "cancel":
                await db.execute(
                    "UPDATE query_jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','running','waiting','paused')",
                    (time.time(), identifier),
                )
            elif raw["status"] in {"paused", "failed"}:
                state = json.loads(raw["progress"])
                state["request_ceiling"] = (
                    state.get("total_requests", 0) + self.options.task_max_requests
                )
                await db.execute(
                    "UPDATE query_jobs SET status='queued',progress=?,error='',next_at=0,updated_at=? WHERE id=? AND status IN ('paused','failed')",
                    (json.dumps(state), time.time(), identifier),
                )
        return await self.admin_get(identifier)

    async def get(self, identifier: str, owner: str) -> dict[str, Any]:
        async with self.store.transaction() as db:
            item = await row(
                db,
                "SELECT id,query,context_json,status,progress,result,error,next_at,notified_at "
                "FROM query_jobs WHERE id=? AND owner_hash=?",
                (identifier, self.owner_token(owner)),
            )
        if not item or json.loads(item["context_json"]).get("_scope") != self.scope:
            raise ValueError("查询任务不存在或凭证范围已变更")
        return self.public(item)

    async def list_jobs(self, owner: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.store.transaction() as db:
            items = await rows(
                db,
                "SELECT id,query,context_json,status,progress,result,error,next_at,notified_at "
                "FROM query_jobs WHERE owner_hash=? ORDER BY updated_at DESC LIMIT ?",
                (self.owner_token(owner), min(max(limit, 1), 100)),
            )
        return [self.public(item) for item in items]

    async def cancel(self, identifier: str, owner: str) -> dict[str, Any]:
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE query_jobs SET status='cancelled',updated_at=? "
                "WHERE id=? AND owner_hash=? AND status IN ('queued','running','waiting','paused')",
                (time.time(), identifier, self.owner_token(owner)),
            )
        return await self.get(identifier, owner)

    async def retry(self, identifier: str, owner: str) -> dict[str, Any]:
        await self.get(identifier, owner)
        async with self.store.transaction() as db:
            raw = await row(
                db,
                "SELECT progress FROM query_jobs WHERE id=? AND owner_hash=?",
                (identifier, self.owner_token(owner)),
            )
            state = json.loads(raw["progress"]) if raw else {}
            state["request_ceiling"] = (
                state.get("total_requests", 0) + self.options.task_max_requests
            )
            await db.execute(
                "UPDATE query_jobs SET progress=? WHERE id=? AND owner_hash=? AND status IN ('paused','failed')",
                (json.dumps(state), identifier, self.owner_token(owner)),
            )
            await db.execute(
                "UPDATE query_jobs SET status='queued',error='',next_at=?,updated_at=? "
                "WHERE id=? AND owner_hash=? AND status IN ('paused','failed')",
                (time.time(), time.time(), identifier, self.owner_token(owner)),
            )
        return await self.get(identifier, owner)

    async def prune(self) -> None:
        now = time.time()
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE query_jobs SET status='cancelled',error='任务已超过7天保留期',updated_at=? WHERE status IN ('queued','running','waiting','paused') AND created_at<?",
                (now, now - 7 * 86400),
            )
            await db.execute(
                "DELETE FROM query_jobs WHERE status IN ('complete','cancelled','failed') AND updated_at<?",
                (now - 86400,),
            )
