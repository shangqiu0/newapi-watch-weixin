"""Privacy-preserving command traces; identities are fingerprints, never permissions."""

import hashlib
import hmac
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .config import Settings
from .operations import Page
from .persistence import row, rows
from .store import Store

Source = Literal["local", "http", "wecom"]
Outcome = Literal["success", "invalid_input", "upstream_error", "conflict", "error", "cancelled"]


class AuditRecord(BaseModel):
    source: Source = "local"
    actor_hash: str = Field(default="", max_length=64)
    action: Literal[
        "channels",
        "users",
        "logs",
        "report",
        "ranking",
        "alerts",
        "notifications",
        "audits",
        "health",
        "help",
        "extension",
    ]
    outcome: Outcome = "success"
    duration_ms: int = Field(default=0, ge=0)


class Audit:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings, self.store = settings, store

    def fingerprint(self, actor: str) -> str:
        if not actor:
            return ""
        key = self.settings.api_key.get_secret_value() or "offline-demo-only"
        return hmac.new(key.encode(), actor.encode(), hashlib.sha256).hexdigest()

    async def record(self, entry: AuditRecord) -> None:
        now = time.time()
        async with self.store.transaction() as db:
            await db.execute(
                """INSERT INTO command_audits
                (id,source,actor_hash,action,outcome,duration_ms,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    entry.source,
                    entry.actor_hash,
                    entry.action,
                    entry.outcome,
                    entry.duration_ms,
                    now,
                    now,
                ),
            )

    async def list(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        paging = Page(page=page, page_size=page_size)
        async with self.store.transaction() as db:
            count = await row(db, "SELECT COUNT(*) AS total FROM command_audits")
            items = await rows(
                db,
                """SELECT id,source,actor_hash,action,outcome,duration_ms,created_at
                FROM command_audits ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
                (paging.page_size, paging.offset),
            )
        return {"items": items, "total": count["total"] if count else 0, **paging.model_dump()}

    async def prune(self, before: float) -> None:
        async with self.store.transaction() as db:
            await db.execute("DELETE FROM command_audits WHERE created_at<?", (before,))
