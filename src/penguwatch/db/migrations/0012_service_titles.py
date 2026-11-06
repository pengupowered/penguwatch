"""0012: services get `titles_json` (exact titles a service carries, from config.yaml `titles:`); the where-to-watch
columns the removed TMDB integration left in shows (`providers_json`, `providers_updated_at`, empty since 0009) are
dropped."""

import aiosqlite


async def _columns(conn: aiosqlite.Connection, table: str) -> set:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def migrate(conn: aiosqlite.Connection) -> None:
    if "titles_json" not in await _columns(conn, "services"):
        await conn.execute("ALTER TABLE services ADD COLUMN titles_json TEXT DEFAULT '[]'")
    shows = await _columns(conn, "shows")
    for column in ("providers_json", "providers_updated_at"):
        if column in shows:
            await conn.execute(f"ALTER TABLE shows DROP COLUMN {column}")
