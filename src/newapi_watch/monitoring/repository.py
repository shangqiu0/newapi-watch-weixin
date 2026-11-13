"""Atomic minimal check/condition state and outbox; network never runs in a transaction."""

import json
from typing import Any

from ..persistence import enqueue, row, rows
from ..store import Store
from .models import Policy
from .service import record_alert


class PolicyRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def check(self, key: str) -> dict[str, Any] | None:
        async with self.store.transaction() as db:
            return await row(
                db,
                "SELECT id,status,last_at,last_ok_at,next_at,failures,error,detail FROM monitor_checks WHERE id=?",
                (key,),
            )

    async def finish(
        self,
        key: str,
        now: float,
        interval: float,
        *,
        error: str = "",
        detail: dict[str, Any] | None = None,
        not_before: float = 0,
        pending: bool = False,
    ) -> None:
        async with self.store.transaction() as db:
            previous = await row(
                db, "SELECT failures,last_ok_at FROM monitor_checks WHERE id=?", (key,)
            )
            failures = int(previous["failures"]) + 1 if error and previous else int(bool(error))
            next_at = now + (min(900, interval * 2 ** min(failures - 1, 5)) if error else interval)
            if pending:
                failures = int(previous["failures"]) if previous else 0
            next_at = max(next_at, not_before)
            last_ok = float(previous["last_ok_at"]) if error and previous else (0 if error else now)
            if pending:
                last_ok = float(previous["last_ok_at"]) if previous else 0
            await db.execute(
                """INSERT INTO monitor_checks(id,status,last_at,last_ok_at,next_at,failures,error,detail,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,last_at=excluded.last_at,
                last_ok_at=excluded.last_ok_at,next_at=excluded.next_at,failures=excluded.failures,error=excluded.error,
                detail=CASE WHEN excluded.status='unknown' AND excluded.detail='{}' THEN monitor_checks.detail ELSE excluded.detail END,updated_at=excluded.updated_at""",
                (
                    key,
                    "unknown" if error else "pending" if pending else "ok",
                    now,
                    last_ok,
                    next_at,
                    failures,
                    error,
                    json.dumps(detail or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    async def condition(
        self,
        key: str,
        active: bool | None,
        text: str,
        policy: Policy,
        kind: str,
        now: float,
        *,
        evidence: dict[str, Any] | None = None,
        repeat_notifications: bool = True,
    ) -> None:
        if active is None:
            async with self.store.transaction() as db:
                await db.execute("UPDATE monitor_states SET streak=0 WHERE id=?", (key,))
            return  # manual-disable breaks a pending streak, but is not proof of recovery
        async with self.store.transaction() as db:
            previous = await row(
                db, "SELECT is_active,streak,last_sent_at FROM monitor_states WHERE id=?", (key,)
            )
            was_active = bool(previous and previous["is_active"])
            streak = (int(previous["streak"]) if previous else 0) + 1 if active else 0
            firing = active and streak >= policy.confirmations
            current_active = (was_active or firing) if active else False
            last_sent = float(previous["last_sent_at"]) if previous else 0
            notify = (
                firing
                and (
                    not was_active
                    or (repeat_notifications and now - last_sent >= policy.cooldown_seconds)
                )
            ) or (was_active and not active and policy.notify_recovery)
            if firing or (was_active and not active):
                await record_alert(db, key, text, current_active, "warning", now)
            if notify:
                dedupe = f"policy:{key}:{int(current_active)}:{now}"
                await enqueue(db, dedupe, text, now)
                await db.execute(
                    "UPDATE notifications SET kind=?,mention_all=?,binding_ids=? WHERE dedupe_key=?",
                    (kind, int(policy.mention_all), json.dumps(policy.binding_ids), dedupe),
                )
                last_sent = now
            await db.execute(
                """INSERT INTO monitor_states(id,is_active,streak,last_sent_at,evidence,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET is_active=excluded.is_active,streak=excluded.streak,
                last_sent_at=excluded.last_sent_at,evidence=excluded.evidence,updated_at=excluded.updated_at""",
                (
                    key,
                    int(current_active),
                    streak,
                    last_sent,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    async def states(self) -> list[dict[str, Any]]:
        async with self.store.transaction() as db:
            return await rows(
                db, "SELECT id,is_active,streak,evidence,updated_at FROM monitor_states"
            )
