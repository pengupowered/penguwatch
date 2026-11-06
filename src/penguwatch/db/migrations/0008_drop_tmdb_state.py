"""0008: delete the TMDB credentials and region left in app_state by the removed TMDB integration."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM app_state WHERE key IN ('tmdb_api_key', 'tmdb_region')")
