"""0009: clear the where-to-watch data the removed TMDB integration left in shows.providers_json / providers_updated_at.

SIMKL does not provide it, so nothing would ever refresh it and the old values would show as stale chips. The columns
stay (dropping them is not worth a table rebuild); they are simply empty from now on."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(shows)")
    existing = {row[1] for row in await cursor.fetchall()}
    if "providers_json" in existing:
        await conn.execute("UPDATE shows SET providers_json = '[]'")
    if "providers_updated_at" in existing:
        await conn.execute("UPDATE shows SET providers_updated_at = NULL")
