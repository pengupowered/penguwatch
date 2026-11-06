"""0015: titles are identified by a set of external ids, one per provider (`title_ids`), instead of fixed columns.

- `title_ids` (show, provider, external id): at most one id per provider for a title. Lookups by (provider, id) use
  their own index; that index is not unique, because catalogues give several of a library's titles the same id (the
  seasons of an anime that a tracker lists as separate titles share one TMDB and TVDB id). The values keep their
  type: numbers as integers, IMDb's "tt..." as text.
- Copied from shows: simkl_id -> "simkl", imdb_id -> "imdb", tmdb_id -> "tmdb", tvdb_id -> "tvdb". Then imdb_id,
  tmdb_id and tvdb_id are dropped. shows.simkl_id stays as an unused column: it is UNIQUE and indexed, so dropping it
  needs a rebuild of `shows`, and a rebuild inside this transaction (foreign keys on) would cascade-delete every
  episode and rating. Nothing reads or writes it any more.
- episodes.simkl_id -> episodes.ids_json ({"simkl": 123}); the old column stays, unused, for the same kind of reason
  (dropping it is safe, but the copy is enough and the column costs nothing).
- shows.sequels_json entries {"simkl_id": n, ...} become {"ids": {"simkl": n}, ...}, and so do the entries of
  changes staged for review (app_state incoming_staged).
- The trending and recommendation caches (app_state) are dropped: their items had the old shape; they are fetched
  again when next asked for.
"""

import json

import aiosqlite

_ID_COLUMNS = (("simkl_id", "simkl"), ("imdb_id", "imdb"), ("tmdb_id", "tmdb"), ("tvdb_id", "tvdb"))


def _normal(value):
    """The value as title_ids stores it: integers for numbers (also numeric text), trimmed text otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and not (len(text) > 1 and text.startswith("0")):
        return int(text) or None
    return text


async def _columns(conn: aiosqlite.Connection, table: str) -> set:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS title_ids (
            show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            external_id NOT NULL,
            PRIMARY KEY (show_id, provider)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_title_ids_lookup ON title_ids(provider, external_id)")

    shows = await _columns(conn, "shows")
    present = [(col, provider) for col, provider in _ID_COLUMNS if col in shows]
    if present:
        cursor = await conn.execute(f"SELECT id, {', '.join(c for c, _ in present)} FROM shows")
        rows = []
        for row in await cursor.fetchall():
            for i, (_, provider) in enumerate(present, start=1):
                value = _normal(row[i])
                if value is not None:
                    rows.append((row[0], provider, value))
        await conn.executemany(
            "INSERT OR IGNORE INTO title_ids (show_id, provider, external_id) VALUES (?, ?, ?)", rows)
    for col in ("imdb_id", "tmdb_id", "tvdb_id"):
        if col in shows:
            await conn.execute(f"ALTER TABLE shows DROP COLUMN {col}")

    episodes = await _columns(conn, "episodes")
    if "ids_json" not in episodes:
        await conn.execute("ALTER TABLE episodes ADD COLUMN ids_json TEXT")
    if "simkl_id" in episodes:
        await conn.execute(
            "UPDATE episodes SET ids_json = json_object('simkl', simkl_id) "
            "WHERE simkl_id IS NOT NULL AND ids_json IS NULL")

    if "sequels_json" in shows:
        cursor = await conn.execute("SELECT id, sequels_json FROM shows WHERE sequels_json IS NOT NULL")
        for show_id, raw in await cursor.fetchall():
            try:
                sequels = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(sequels, list):
                continue
            changed = False
            out = []
            for sq in sequels:
                if isinstance(sq, dict) and "simkl_id" in sq:
                    sq = dict(sq)
                    value = _normal(sq.pop("simkl_id"))
                    sq = {"ids": {"simkl": value} if value is not None else {}, **sq}
                    changed = True
                out.append(sq)
            if changed:
                await conn.execute("UPDATE shows SET sequels_json = ? WHERE id = ?", (json.dumps(out), show_id))

    cursor = await conn.execute("SELECT value FROM app_state WHERE key = 'incoming_staged'")
    row = await cursor.fetchone()
    if row is not None:
        try:
            staged = json.loads(row[0])
        except ValueError:
            staged = None
        if isinstance(staged, dict) and isinstance(staged.get("entries"), list):
            entries = []
            for entry in staged["entries"]:
                if isinstance(entry, dict) and "simkl_id" in entry:
                    entry = dict(entry)
                    value = _normal(entry.pop("simkl_id"))
                    entry["ids"] = {"simkl": value} if value is not None else {}
                entries.append(entry)
            staged["entries"] = entries
            await conn.execute("UPDATE app_state SET value = ? WHERE key = 'incoming_staged'", (json.dumps(staged),))
    await conn.execute("DELETE FROM app_state WHERE key LIKE 'trending:%' OR key = 'recs_cache_simkl'")
