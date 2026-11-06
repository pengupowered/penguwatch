"""0003: index for watched-episode lookups."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_watched ON episodes(is_watched, watched_at)"
    )
