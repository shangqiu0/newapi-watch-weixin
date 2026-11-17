"""Shared single-permission alert operations; no direct outbound network calls."""

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .persistence import row, rows
from .store import Store


class OperationError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Page(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Operations:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def alerts(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        paging = Page(page=page, page_size=page_size)
        async with self.store.transaction() as db:
            hidden = "NOT EXISTS (SELECT 1 FROM metadata WHERE id='history:hidden:'||a.id AND value=CAST(a.generation AS TEXT))"
            count = await row(db, "SELECT COUNT(*) AS total FROM alerts a WHERE " + hidden)
            items = await rows(
                db,
                """SELECT a.id,a.key,a.state,a.title,a.level,a.generation,a.created_at,a.updated_at,
                CASE WHEN k.id IS NULL THEN 0 ELSE 1 END AS acknowledged
                FROM alerts a LEFT JOIN alert_acknowledgements k ON k.alert_id=a.id AND k.generation=a.generation
                WHERE """
                + hidden
                + """ ORDER BY a.updated_at DESC,a.id LIMIT ? OFFSET ?""",
                (paging.page_size, paging.offset),
            )
        for item in items:
            item["acknowledged"] = bool(item["acknowledged"])
        return {"items": items, "total": count["total"] if count else 0, **paging.model_dump()}

    async def acknowledge(
        self, alert_id: str, *, actor_hash: str = "", now: float | None = None
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        async with self.store.transaction() as db:
            alert = await row(db, "SELECT id,state,generation FROM alerts WHERE id=?", (alert_id,))
            if alert is None:
                raise OperationError(404, "告警不存在")
            if alert["state"] != "firing":
                raise OperationError(409, "告警已恢复，无需确认")
            cursor = await db.execute(
                """INSERT OR IGNORE INTO alert_acknowledgements
                (id,alert_id,generation,actor_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)""",
                (uuid4().hex, alert_id, alert["generation"], actor_hash, now, now),
            )
            return {
                "id": alert_id,
                "generation": alert["generation"],
                "acknowledged": True,
                "duplicate": cursor.rowcount == 0,
            }

    async def silences(
        self, *, now: float | None = None, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        paging = Page(page=page, page_size=page_size)
        async with self.store.transaction() as db:
            count = await row(db, "SELECT COUNT(*) AS total FROM silences WHERE until_at>?", (now,))
            items = await rows(
                db,
                """SELECT id AS channel_id,until_at,created_at,updated_at FROM silences
                WHERE until_at>? ORDER BY until_at,id LIMIT ? OFFSET ?""",
                (now, paging.page_size, paging.offset),
            )
        return {"items": items, "total": count["total"] if count else 0, **paging.model_dump()}

    async def dead_letters(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        paging = Page(page=page, page_size=page_size)
        async with self.store.transaction() as db:
            count = await row(db, "SELECT COUNT(*) AS total FROM notifications WHERE status='dead'")
            items = await rows(
                db,
                """SELECT id,status,attempts,last_error,created_at,updated_at FROM notifications
                WHERE status='dead' ORDER BY id LIMIT ? OFFSET ?""",
                (paging.page_size, paging.offset),
            )
        return {"items": items, "total": count["total"] if count else 0, **paging.model_dump()}

    async def mark_pending_delivered(
        self, request_key: str, *, before_id: int, now: float | None = None
    ) -> dict[str, Any]:
        """Operator-only maintenance: never contacts Webhooks or fabricates a receipt."""
        if not 8 <= len(request_key) <= 256 or before_id < 0:
            raise OperationError(400, "操作幂等键或通知快照上限无效")
        now = time.time() if now is None else now
        key = "notification_manual_mark:" + hashlib.sha256(request_key.encode()).hexdigest()
        async with self.store.transaction() as db:
            previous = await row(db, "SELECT value FROM metadata WHERE id=?", (key,))
            if previous:
                result = json.loads(previous["value"])
                if result["before_id"] != before_id:
                    raise OperationError(409, "同一操作幂等键不能用于不同通知快照")
                return {**result, "duplicate": True}
            changed = await db.execute(
                """UPDATE notifications SET status='sent',lease_until=0,
                last_error='管理员人工标记已投递（未实际发送）',next_at=?,updated_at=?
                WHERE id<=? AND status='pending' AND kind IN ('channel','request_error','balance','service')""",
                (now, now, before_id),
            )
            result = {
                "count": changed.rowcount,
                "before_id": before_id,
                "marked_at": now,
                "duplicate": False,
            }
            await self.store.put_meta(db, key, json.dumps(result), now)
            await db.execute(
                """INSERT INTO command_audits(id,source,action,outcome,created_at,updated_at)
                VALUES(?,'local','notifications_manual_mark','success',?,?)""",
                (uuid4().hex, now, now),
            )
            return result

    async def retry(
        self, notification_id: int, request_key: str, *, now: float | None = None
    ) -> dict[str, Any]:
        if notification_id <= 0 or not 8 <= len(request_key) <= 256:
            raise OperationError(400, "通知ID或幂等键无效")
        now = time.time() if now is None else now
        fingerprint = hashlib.sha256(request_key.encode()).hexdigest()
        async with self.store.transaction() as db:
            prior = await row(
                db, "SELECT notification_id FROM notification_retries WHERE id=?", (fingerprint,)
            )
            if prior:
                if prior["notification_id"] != notification_id:
                    raise OperationError(409, "同一幂等键不能用于其他通知")
                return {"id": notification_id, "duplicate": True, "status": "already_requested"}
            notification = await row(
                db, "SELECT id,status,content FROM notifications WHERE id=?", (notification_id,)
            )
            if notification is None:
                raise OperationError(404, "通知不存在")
            if notification["status"] not in {"dead", "historical"}:
                raise OperationError(409, "仅支持重试投递失败或历史停发的通知")
            content = str(notification["content"])
            if not content.startswith("[历史通知重试]"):
                content = "[历史通知重试]\n" + content
            await db.execute(
                """UPDATE notifications SET status='pending',attempts=0,next_at=?,lease_until=0,
                last_error='',content=?,updated_at=? WHERE id=? AND status IN ('dead','historical')""",
                (now, content, now, notification_id),
            )
            await db.execute(
                "INSERT INTO notification_retries(id,notification_id,created_at,updated_at) VALUES(?,?,?,?)",
                (fingerprint, notification_id, now, now),
            )
            return {"id": notification_id, "duplicate": False, "status": "pending"}
