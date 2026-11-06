"""0002: add columns introduced after the first release, guarded by PRAGMA table_info."""

import aiosqlite

from penguwatch.db.schema import OUTBOX_COLUMN_MIGRATIONS, SHOW_COLUMN_MIGRATIONS


async def _existing_columns(conn: aiosqlite.Connection, table: str) -> set:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _add_missing(conn: aiosqlite.Connection, table: str, definitions: list) -> list:
    existing = await _existing_columns(conn, table)
    added = []
    for definition in definitions:
        column = definition.split()[0]
        if column not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            added.append(column)
    return added


async def migrate(conn: aiosqlite.Connection) -> None:
    added = await _add_missing(conn, "shows", SHOW_COLUMN_MIGRATIONS)
    await _add_missing(conn, "sync_outbox", OUTBOX_COLUMN_MIGRATIONS)
    if "media_type" in added:
        # Back-fill media type for libraries created before movies/anime were distinguished
        await conn.execute(
            "UPDATE shows SET media_type = 'anime' WHERE genres_json LIKE '%\"Anime\"%'"
        )
