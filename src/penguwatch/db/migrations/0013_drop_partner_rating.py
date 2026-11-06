"""0013: drop shows.partner_rating. It had no screen, was empty in practice, and only the interest score and the CSV
export read it; the interest score now uses your own rating alone."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(shows)")
    if "partner_rating" in {row[1] for row in await cursor.fetchall()}:
        await conn.execute("ALTER TABLE shows DROP COLUMN partner_rating")
