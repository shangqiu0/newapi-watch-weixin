"""Additive schema upgrades. Roll back a deployment by restoring its pre-upgrade backup."""

from pathlib import Path

import aiosqlite

from .persistence import row

SCHEMA_VERSION = 4


async def migrate(db: aiosqlite.Connection) -> None:
    current = await row(db, "PRAGMA user_version")
    version = int(current["user_version"]) if current else 0
    if version > SCHEMA_VERSION:
        raise RuntimeError("数据库版本高于本程序，拒绝覆盖")
    directory = Path(__file__).parent
    if version == 0:
        await db.executescript(directory.joinpath("schema.sql").read_text("utf-8-sig"))
        version = 1
    if version == 1:
        try:
            await db.executescript(
                "BEGIN IMMEDIATE;\n"
                + directory.joinpath("002.sql").read_text("utf-8-sig")
                + "\nCOMMIT;"
            )
        except BaseException:
            await db.rollback()
            raise
    if version <= 2:
        try:
            await db.executescript(
                "BEGIN IMMEDIATE;\n"
                + directory.joinpath("003.sql").read_text("utf-8-sig")
                + "\nCOMMIT;"
            )
        except BaseException:
            await db.rollback()
            raise
    if version <= 3:
        try:
            await db.executescript(
                "BEGIN IMMEDIATE;\n"
                + directory.joinpath("004.sql").read_text("utf-8-sig")
                + "\nCOMMIT;"
            )
        except BaseException:
            await db.rollback()
            raise
    await db.commit()
