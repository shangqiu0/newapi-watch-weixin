"""Additive control-registry migrations; restore pre-upgrade backup to roll back."""

from pathlib import Path

import aiosqlite

from .persistence import row


async def migrate_registry(db: aiosqlite.Connection) -> None:
    current = await row(db, "PRAGMA user_version")
    version = int(current["user_version"]) if current else 0
    if version > 3:
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
    if version <= 2:
        try:
            await db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS operation_records (
                    id TEXT PRIMARY KEY, platform_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '', object_id TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'success',
                    created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_operations_platform_time ON operation_records(platform_id,created_at);
                PRAGMA user_version=3;
                COMMIT;
            """)
        except BaseException:
            await db.rollback()
            raise
    await db.commit()
