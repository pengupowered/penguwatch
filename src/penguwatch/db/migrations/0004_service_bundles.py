"""0004: services.includes_json holds the ids of the services a bundle includes."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(services)")
    existing = {row[1] for row in await cursor.fetchall()}
    if "includes_json" not in existing:
        await conn.execute("ALTER TABLE services ADD COLUMN includes_json TEXT DEFAULT '[]'")
