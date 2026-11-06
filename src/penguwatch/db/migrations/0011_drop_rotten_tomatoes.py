"""0011: drop shows.rt_rating. The Rotten Tomatoes scraper is gone and nothing else wrote or read the column."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(shows)")
    if "rt_rating" in {row[1] for row in await cursor.fetchall()}:
        await conn.execute("ALTER TABLE shows DROP COLUMN rt_rating")
