"""0007: shows.en_title, shows.alt_titles_json (NULL = never checked) and shows.search_text, filled for existing rows."""

import aiosqlite

from penguwatch.db.titles import search_text_for_row

_COLUMNS = ("en_title TEXT", "alt_titles_json TEXT", "search_text TEXT")


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(shows)")
    existing = {row[1] for row in await cursor.fetchall()}
    for definition in _COLUMNS:
        if definition.split()[0] not in existing:
            await conn.execute(f"ALTER TABLE shows ADD COLUMN {definition}")
    cursor = await conn.execute("SELECT id, title, en_title, alt_titles_json, network, genres_json FROM shows")
    for show_id, *source in await cursor.fetchall():
        await conn.execute("UPDATE shows SET search_text = ? WHERE id = ?", (search_text_for_row(source), show_id))
