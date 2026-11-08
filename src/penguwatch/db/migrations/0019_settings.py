"""0019: the settings store (penguwatch.db.settings): the stored values, the list items (webhooks) and the kept versions;
and the services' "edited by you" marker (`user_edited`: 1 once the user changed a service's name, aliases, titles or
bundle members, so a release's catalogue never overwrites them; db/services.py). Every existing service starts at 0;
the one-time settings import marks the ones that are the user's own (penguwatch.settings.importer).

Tables only: the import runs in the settings component, which needs the config. A build before 0.16 ignores these
tables (its runner applies only the migrations it knows). The secrets are not here: they live in secrets.json next to
the database (penguwatch.secrets)."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute("CREATE TABLE IF NOT EXISTS settings_values (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                       "updated REAL NOT NULL)")
    await conn.execute("CREATE TABLE IF NOT EXISTS settings_items (list TEXT NOT NULL, id TEXT NOT NULL, "
                       "position INTEGER NOT NULL, config TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (list, id))")
    await conn.execute("CREATE TABLE IF NOT EXISTS settings_versions (revision INTEGER PRIMARY KEY, "
                       "applied_at REAL NOT NULL, source TEXT NOT NULL, summary TEXT NOT NULL, document TEXT NOT NULL)")
    cursor = await conn.execute("PRAGMA table_info(services)")
    if "user_edited" not in {row[1] for row in await cursor.fetchall()}:
        await conn.execute("ALTER TABLE services ADD COLUMN user_edited INTEGER NOT NULL DEFAULT 0")
