"""Tests for the numbered migration runner."""

import aiosqlite
import pytest

from penguwatch.db import migrations
from penguwatch.db.migrations import MIGRATIONS, run_migrations
from penguwatch.db.schema import OUTBOX_COLUMN_MIGRATIONS, SHOW_COLUMN_MIGRATIONS

VERSIONS = [v for v, _, _ in MIGRATIONS]

LEGACY_SQL = """
CREATE TABLE services (id TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_cost REAL DEFAULT 0.0,
    is_active INTEGER DEFAULT 0, renewal_day INTEGER, renewal_date TEXT,
    aliases_json TEXT DEFAULT '[]', notes TEXT);
CREATE TABLE shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, simkl_id INTEGER UNIQUE, imdb_id TEXT, tmdb_id INTEGER,
    tvdb_id INTEGER, title TEXT NOT NULL, year INTEGER, poster TEXT, banner TEXT, overview TEXT,
    genres_json TEXT DEFAULT '[]', network TEXT, service_id TEXT, status TEXT DEFAULT 'plantowatch',
    priority TEXT DEFAULT 'medium', household_tag TEXT DEFAULT 'both', simkl_rating REAL,
    imdb_rating REAL, imdb_votes INTEGER, user_rating REAL, partner_rating REAL,
    total_episodes INTEGER DEFAULT 0, watched_episodes INTEGER DEFAULT 0,
    aired_episodes INTEGER DEFAULT 0, runtime_minutes INTEGER DEFAULT 45,
    next_episode_air_date TEXT, next_episode_season INTEGER, next_episode_number INTEGER,
    next_episode_title TEXT, is_ended INTEGER DEFAULT 0, notes TEXT, last_watched_at TEXT,
    created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE sync_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, show_id INTEGER, simkl_id INTEGER,
    op TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER DEFAULT 0, last_error TEXT,
    next_attempt_at TEXT, created_at TEXT DEFAULT (datetime('now')));
"""


async def _columns(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in await cur.fetchall()}


async def _versions(conn):
    cur = await conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    return [r[0] for r in await cur.fetchall()]


async def _names(conn, kind):
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {r[0] for r in await cur.fetchall()}


@pytest.fixture
async def conn():
    c = await aiosqlite.connect(":memory:")
    yield c
    await c.close()


async def test_fresh_database_reaches_latest_version(conn):
    applied = await run_migrations(conn)
    assert applied == VERSIONS
    assert await _versions(conn) == VERSIONS
    tables = await _names(conn, "table")
    assert {"services", "shows", "episodes", "app_state", "sync_outbox", "schema_migrations"} <= tables
    show_cols = await _columns(conn, "shows")
    assert {c.split()[0] for c in SHOW_COLUMN_MIGRATIONS} <= show_cols
    assert {c.split()[0] for c in OUTBOX_COLUMN_MIGRATIONS} <= await _columns(conn, "sync_outbox")
    assert "idx_episodes_watched" in await _names(conn, "index")


async def test_legacy_database_is_upgraded_and_keeps_data(conn):
    await conn.executescript(LEGACY_SQL)
    await conn.execute(
        "INSERT INTO shows (simkl_id, title, genres_json) VALUES (1, 'Cowboy Bebop', '[\"Anime\", \"Action\"]')"
    )
    await conn.execute(
        "INSERT INTO shows (simkl_id, title, genres_json) VALUES (2, 'The Wire', '[\"Drama\"]')"
    )
    await conn.execute("INSERT INTO sync_outbox (op, payload_json) VALUES ('x', '{}')")
    await conn.commit()
    assert "media_type" not in await _columns(conn, "shows")

    assert await run_migrations(conn) == VERSIONS

    assert {c.split()[0] for c in SHOW_COLUMN_MIGRATIONS} <= await _columns(conn, "shows")
    assert {"approved", "summary"} <= await _columns(conn, "sync_outbox")
    cur = await conn.execute("SELECT title, media_type FROM shows ORDER BY id")
    assert [tuple(r) for r in await cur.fetchall()] == [("Cowboy Bebop", "anime"), ("The Wire", "show")]
    cur = await conn.execute("SELECT op, approved FROM sync_outbox")
    assert [tuple(r) for r in await cur.fetchall()] == [("x", 0)]


async def test_second_run_applies_nothing(conn):
    await run_migrations(conn)
    assert await run_migrations(conn) == []
    assert await _versions(conn) == VERSIONS


async def test_failing_migration_rolls_back_and_records_nothing(conn, monkeypatch):
    await run_migrations(conn)

    async def bad(c):
        await c.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS + [(len(MIGRATIONS) + 1, "bad", bad)])
    with pytest.raises(RuntimeError):
        await run_migrations(conn)
    assert "half_done" not in await _names(conn, "table")
    assert await _versions(conn) == VERSIONS
    assert not conn.in_transaction


def test_versions_are_contiguous():
    assert [v for v, _, _ in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


async def test_0008_deletes_tmdb_state_and_keeps_the_rest(conn):
    await run_migrations(conn)
    await conn.execute("DELETE FROM schema_migrations WHERE version = 8")
    for key in ("tmdb_api_key", "tmdb_region", "simkl_last_sync"):
        await conn.execute("INSERT INTO app_state (key, value) VALUES (?, 'x')", (key,))
    await conn.commit()

    assert await run_migrations(conn) == [8]

    cur = await conn.execute("SELECT key FROM app_state")
    assert [r[0] for r in await cur.fetchall()] == ["simkl_last_sync"]


async def test_0009_clears_stale_providers_and_keeps_simkl_ids(conn):
    await run_migrations(conn)
    await conn.execute("DELETE FROM schema_migrations WHERE version = 9")
    # the columns as a database from before 0012 has them (0012 drops them)
    await conn.execute("ALTER TABLE shows ADD COLUMN providers_json TEXT DEFAULT '[]'")
    await conn.execute("ALTER TABLE shows ADD COLUMN providers_updated_at TEXT")
    await conn.execute(
        "INSERT INTO shows (title, simkl_id, providers_json, providers_updated_at) "
        "VALUES ('Old', 1, '[{\"name\": \"Netflix\", \"type\": \"flatrate\"}]', '2026-01-01T00:00:00')")
    await conn.commit()

    assert await run_migrations(conn) == [9]

    cur = await conn.execute("SELECT providers_json, providers_updated_at, simkl_id FROM shows")
    assert [tuple(r) for r in await cur.fetchall()] == [("[]", None, 1)]


async def test_0011_drops_rt_rating_and_keeps_the_row(conn):
    await run_migrations(conn)
    await conn.execute("DELETE FROM schema_migrations WHERE version = 11")
    await conn.execute("ALTER TABLE shows ADD COLUMN rt_rating INTEGER")  # as in a database from before 0.12
    await conn.execute("INSERT INTO shows (title, simkl_id, imdb_rating, rt_rating) VALUES ('Old', 1, 8.1, 91)")
    await conn.commit()

    assert await run_migrations(conn) == [11]

    assert "rt_rating" not in await _columns(conn, "shows")
    cur = await conn.execute("SELECT title, simkl_id, imdb_rating FROM shows")
    assert [tuple(r) for r in await cur.fetchall()] == [("Old", 1, 8.1)]


async def test_fresh_database_has_no_rt_rating(conn):
    await run_migrations(conn)
    assert "rt_rating" not in await _columns(conn, "shows")


async def test_0012_adds_service_titles_and_drops_the_provider_columns(conn):
    await run_migrations(conn)
    await conn.execute("DELETE FROM schema_migrations WHERE version = 12")
    await conn.execute("ALTER TABLE services DROP COLUMN titles_json")  # as before 0012
    await conn.execute("ALTER TABLE shows ADD COLUMN providers_json TEXT DEFAULT '[]'")
    await conn.execute("ALTER TABLE shows ADD COLUMN providers_updated_at TEXT")
    await conn.execute("INSERT INTO services (id, name) VALUES ('netflix', 'Netflix')")
    await conn.execute("INSERT INTO shows (title, simkl_id, service_id, providers_json) VALUES ('Old', 1, 'netflix', '[]')")
    await conn.commit()

    assert await run_migrations(conn) == [12]

    assert not {"providers_json", "providers_updated_at"} & await _columns(conn, "shows")
    cur = await conn.execute("SELECT s.title, s.service_id, v.titles_json FROM shows s JOIN services v ON v.id = s.service_id")
    assert [tuple(r) for r in await cur.fetchall()] == [("Old", "netflix", "[]")]


async def test_fresh_database_has_titles_and_no_provider_columns(conn):
    await run_migrations(conn)
    assert "titles_json" in await _columns(conn, "services")
    assert not {"providers_json", "providers_updated_at", "rt_rating"} & await _columns(conn, "shows")


async def test_0013_drops_partner_rating_and_keeps_the_row(conn):
    await run_migrations(conn)
    await conn.execute("DELETE FROM schema_migrations WHERE version = 13")
    await conn.execute("ALTER TABLE shows ADD COLUMN partner_rating REAL")  # as in a database from before 0.14
    await conn.execute("INSERT INTO shows (title, simkl_id, user_rating, partner_rating) VALUES ('Old', 1, 8.0, 7.0)")
    await conn.commit()

    assert await run_migrations(conn) == [13]

    assert "partner_rating" not in await _columns(conn, "shows")
    cur = await conn.execute("SELECT title, simkl_id, user_rating FROM shows")
    assert [tuple(r) for r in await cur.fetchall()] == [("Old", 1, 8.0)]


async def test_fresh_database_has_no_partner_rating(conn):
    await run_migrations(conn)
    assert "partner_rating" not in await _columns(conn, "shows")


async def test_0014_creates_the_rating_tables_with_cascades(conn):
    await run_migrations(conn)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("INSERT INTO shows (title, simkl_id) VALUES ('A', 1)")
    await conn.execute("INSERT INTO rating_sources (id, name, label) VALUES ('lb', 'Letterboxd', 'LB')")
    await conn.execute("INSERT INTO title_ratings (show_id, source_id, value) VALUES (1, 'lb', 4.5)")
    await conn.commit()
    assert {"show_id", "source_id", "value", "votes", "url", "updated_at"} <= await _columns(conn, "title_ratings")
    assert "idx_title_ratings_source_value" in await _names(conn, "index")
    await conn.execute("DELETE FROM shows WHERE id = 1")  # a title's values go with it
    assert (await (await conn.execute("SELECT COUNT(*) FROM title_ratings")).fetchone())[0] == 0
    await conn.execute("INSERT INTO shows (title, simkl_id) VALUES ('B', 2)")
    await conn.execute("INSERT INTO title_ratings (show_id, source_id, value) VALUES (2, 'lb', 3)")
    await conn.execute("DELETE FROM rating_sources WHERE id = 'lb'")  # and so do a source's
    assert (await (await conn.execute("SELECT COUNT(*) FROM title_ratings")).fetchone())[0] == 0


# --- 0015: title_ids ------------------------------------------------------------------------------------------------

async def _at_version(conn, monkeypatch, version):
    """The database as migrations up to `version` leave it (the later ones run when the patch is undone)."""
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in MIGRATIONS if m[0] <= version])
    await run_migrations(conn)
    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS)


async def test_0015_copies_every_external_id_into_title_ids(conn, monkeypatch):
    import json
    await _at_version(conn, monkeypatch, 14)
    await conn.executemany(
        "INSERT INTO shows (id, title, simkl_id, imdb_id, tmdb_id, tvdb_id, media_type) VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (1, "Full", 101, "tt0000101", 201, 301, "show"),
            (2, "Season 2", 102, "tt0000102", 201, 301, "anime"),  # the catalogues share ids between seasons
            (3, "Text ids", 103, " tt0000103 ", "202", "0303", "show"),  # the INTEGER columns already made these numbers
            (4, "Blank", 104, "", None, None, "movie"),
            (5, "Local only", None, None, 205, None, "show"),
        ])
    await conn.execute("INSERT INTO episodes (show_id, simkl_id, season, number, is_watched) VALUES (1, 9001, 1, 1, 1)")
    await conn.execute("INSERT INTO episodes (show_id, simkl_id, season, number, is_watched) VALUES (1, NULL, 1, 2, 0)")
    await conn.execute("UPDATE shows SET sequels_json = ? WHERE id = 2", (json.dumps(
        [{"simkl_id": 106, "title": "Season 3", "year": 2027, "poster": None, "anime_type": "tv"}]),))
    await conn.execute("INSERT INTO app_state (key, value) VALUES ('incoming_staged', ?)", (json.dumps({
        "activities": {"all": "x"}, "at": "t", "entries": [
            {"key": "show:101", "kind": "update", "media": "show", "simkl_id": 101, "show_id": 1, "title": "Full",
             "changes": []}]}),))
    await conn.executemany("INSERT INTO app_state (key, value) VALUES (?, ?)", [
        ("trending:tv:week", "{}"), ("recs_cache_simkl", "{}"), ("simkl_last_sync", "kept")])
    await conn.commit()

    assert await run_migrations(conn) == [v for v in VERSIONS if v >= 15]

    cur = await conn.execute(
        "SELECT show_id, provider, external_id, typeof(external_id) FROM title_ids ORDER BY show_id, provider")
    assert [tuple(r) for r in await cur.fetchall()] == [
        (1, "imdb", "tt0000101", "text"), (1, "simkl", 101, "integer"), (1, "tmdb", 201, "integer"),
        (1, "tvdb", 301, "integer"),
        (2, "imdb", "tt0000102", "text"), (2, "simkl", 102, "integer"), (2, "tmdb", 201, "integer"),
        (2, "tvdb", 301, "integer"),
        (3, "imdb", "tt0000103", "text"), (3, "simkl", 103, "integer"), (3, "tmdb", 202, "integer"),
        (3, "tvdb", 303, "integer"),
        (4, "simkl", 104, "integer"),
        (5, "tmdb", 205, "integer"),
    ]
    cols = await _columns(conn, "shows")
    assert not {"imdb_id", "tmdb_id", "tvdb_id"} & cols
    assert "simkl_id" in cols  # UNIQUE: dropping it needs a table rebuild; it stays, unused
    assert "idx_title_ids_lookup" in await _names(conn, "index")
    cur = await conn.execute("SELECT number, ids_json FROM episodes ORDER BY number")
    assert [tuple(r) for r in await cur.fetchall()] == [(1, '{"simkl":9001}'), (2, None)]
    cur = await conn.execute("SELECT sequels_json FROM shows WHERE id = 2")
    assert json.loads((await cur.fetchone())[0]) == [
        {"ids": {"simkl": 106}, "title": "Season 3", "year": 2027, "poster": None, "anime_type": "tv"}]
    cur = await conn.execute("SELECT key, value FROM app_state ORDER BY key")
    state = {k: v for k, v in await cur.fetchall()}
    assert set(state) == {"incoming_staged", "driver.simkl.last_sync"}  # the caches of the old shape are gone
    entry = json.loads(state["incoming_staged"])["entries"][0]
    assert entry["ids"] == {"simkl": 101} and "simkl_id" not in entry and entry["show_id"] == 1


async def test_0015_title_ids_go_with_their_title(conn):
    await run_migrations(conn)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("INSERT INTO shows (id, title) VALUES (1, 'A')")
    await conn.execute("INSERT INTO title_ids (show_id, provider, external_id) VALUES (1, 'imdb', 'tt1')")
    with pytest.raises(aiosqlite.IntegrityError):  # one id per provider and title
        await conn.execute("INSERT INTO title_ids (show_id, provider, external_id) VALUES (1, 'imdb', 'tt2')")
    await conn.execute("DELETE FROM shows WHERE id = 1")
    assert (await (await conn.execute("SELECT COUNT(*) FROM title_ids")).fetchone())[0] == 0


async def test_0016_turns_outbox_rows_into_neutral_ops_and_keeps_odd_ones(conn, monkeypatch):
    import json
    await _at_version(conn, monkeypatch, 15)
    await conn.execute("INSERT INTO shows (id, title, media_type) VALUES (1, 'A', 'anime')")
    await conn.executemany(
        "INSERT INTO sync_outbox (id, show_id, simkl_id, op, payload_json, approved) VALUES (?, ?, ?, ?, ?, 1)", [
            (1, 1, 7, "history_add", json.dumps({"shows": [{"ids": {"simkl": 7}, "seasons": [
                {"number": 1, "episodes": [{"number": 2, "watched_at": "t"}]}]}]})),
            (2, 99, 8, "remove_item", json.dumps({"movies": [{"ids": {"simkl": 8}}]})),  # its title is gone
            (3, None, 9, "mystery", "not json"),
        ])
    await conn.commit()
    assert await run_migrations(conn) == [v for v in VERSIONS if v >= 16]
    cols = await _columns(conn, "sync_outbox")
    assert {"driver", "remote_id"} <= cols and "simkl_id" not in cols
    rows = [tuple(r) for r in await (await conn.execute(
        "SELECT id, driver, remote_id, op, payload_json FROM sync_outbox ORDER BY id")).fetchall()]
    ops = {r[0]: json.loads(r[4]) for r in rows}
    assert [r[:4] for r in rows] == [(1, "simkl", 7, "watched"), (2, "simkl", 8, "remove"), (3, "simkl", 9, "mystery")]
    assert ops[1]["media_type"] == "anime" and ops[1]["episodes"] == [[1, 2]] and ops[1]["watched_at"] == "t"
    assert ops[2]["media_type"] == "movie" and ops[2]["native"] == {"movies": [{"ids": {"simkl": 8}}]}
    assert ops[3] == {"kind": "mystery", "media_type": "show", "ids": {}, "native": {}}
    assert "idx_outbox_remote" in await _names(conn, "index")


async def test_0017_moves_every_simkl_key_under_the_drivers_namespace(conn, monkeypatch):
    from penguwatch.db.migrations import MIGRATIONS as ALL
    renames = dict(__import__("importlib").import_module("penguwatch.db.migrations.0017_driver_state").RENAMES)
    await _at_version(conn, monkeypatch, 16)
    await conn.executemany("INSERT INTO app_state (key, value) VALUES (?, ?)",
                           [(old, f"value of {old}") for old in renames] + [("sync_review_mode", "off"),
                                                                             ("schedule_last_sync", "t")])
    await conn.commit()
    assert await run_migrations(conn) == [v for v, _, _ in ALL if v >= 17]
    state = dict(await (await conn.execute("SELECT key, value FROM app_state")).fetchall())
    assert state == {**{new: f"value of {old}" for old, new in renames.items()},
                     "sync_review_mode": "off", "schedule_last_sync": "t"}
    assert not [k for k in state if k.startswith("simkl_")]
