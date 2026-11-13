"""Low-level parameterized SQL and atomic alert/outbox transitions."""

from collections.abc import Iterable
from typing import Any
from uuid import uuid4

import aiosqlite


async def row(
    db: aiosqlite.Connection, sql: str, params: Iterable[Any] = ()
) -> dict[str, Any] | None:
    async with db.execute(sql, tuple(params)) as cursor:
        result = await cursor.fetchone()
        return dict(result) if result is not None else None


async def rows(
    db: aiosqlite.Connection, sql: str, params: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    async with db.execute(sql, tuple(params)) as cursor:
        return [dict(item) for item in await cursor.fetchall()]


async def enqueue(
    db: aiosqlite.Connection, key: str, content: str, now: float, image: bytes | None = None
) -> None:
    await db.execute(
        """INSERT OR IGNORE INTO notifications
        (dedupe_key,content,image,next_at,created_at,updated_at) VALUES (?,?,?,?,?,?)""",
        (key, content, image, now, now, now),
    )


async def observe(
    db: aiosqlite.Connection, key: str, active: bool, title: str, level: str, now: float
) -> None:
    previous = await row(db, "SELECT id,state,generation FROM alerts WHERE key=?", (key,))
    if not active and (previous is None or previous["state"] == "resolved"):
        return
    if active and previous and previous["state"] == "firing":
        await db.execute("UPDATE alerts SET updated_at=? WHERE key=?", (now, key))
        return
    generation = (
        int(previous["generation"]) + 1
        if previous and active
        else (int(previous["generation"]) if previous else 1)
    )
    state = "firing" if active else "resolved"
    alert_id = previous["id"] if previous else uuid4().hex
    await db.execute(
        """INSERT INTO alerts (id,key,state,title,level,generation,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
        state=excluded.state,title=excluded.title,level=excluded.level,
        generation=excluded.generation,updated_at=excluded.updated_at""",
        (alert_id, key, state, title, level, generation, now, now),
    )
    channel_id = int(key.split(":")[1]) if key.startswith("channel:") else 0
    silence = await row(db, "SELECT until_at FROM silences WHERE id=?", (channel_id,))
    if silence and silence["until_at"] > now:
        return
    heading = "告警" if active else "恢复"
    dedupe_key = f"alert:{alert_id}:{generation}:{state}"
    await enqueue(db, dedupe_key, f"**{heading} · {level}**\n{title}", now)
    await db.execute(
        "UPDATE notifications SET kind=? WHERE dedupe_key=?",
        ("service" if key.startswith("service:") else "channel", dedupe_key),
    )
