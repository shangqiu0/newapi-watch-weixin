"""Additive control-registry migrations; restore pre-upgrade backup to roll back."""

from pathlib import Path

import aiosqlite

from .persistence import row


async def migrate_registry(db: aiosqlite.Connection) -> None:
    current = await row(db, "PRAGMA user_version")
    version = int(current["user_version"]) if current else 0
    if version > 2:
        raise RuntimeError("控制库版本高于当前程序，拒绝降级")
    directory = Path(__file__).parent
    if version == 0:
        await db.executescript(directory.joinpath("registry.sql").read_text("utf-8-sig"))
        version = 1
    if version == 1:
        try:
            await db.executescript(
                "BEGIN IMMEDIATE;\n"
                + directory.joinpath("registry_002.sql").read_text("utf-8-sig")
                + "\nCOMMIT;"
            )
        except BaseException:
            await db.rollback()
            raise
    await db.commit()
