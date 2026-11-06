"""0014: rating sources and the ratings users bring (`rating_sources`, `title_ratings`)."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rating_sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            scale_min REAL NOT NULL DEFAULT 0,
            scale_max REAL NOT NULL DEFAULT 10,
            decimals INTEGER NOT NULL DEFAULT 1,
            show_on_cards INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT 'api',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS title_ratings (
            show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES rating_sources(id) ON DELETE CASCADE,
            value REAL NOT NULL,
            votes INTEGER,
            url TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (show_id, source_id)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_title_ratings_source_value ON title_ratings(source_id, value)")
