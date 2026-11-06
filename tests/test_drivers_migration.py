"""The driver layer's migrations keep everything a library holds.

Two databases from before the driver layer are migrated: the sanitized seed library (.seed, a legacy schema; with
scripts/test.sh --seed) and tests/data/drivers/pre_drivers.sql, a credential-free library at schema 14 (what a
current install has) with pending outbox rows of every kind, login state and staged changes. It was written by the
pre-driver code (tests/data/drivers/generate_pre_drivers.py, run on the commit before the driver layer).

Every title keeps every external id, every episode and its watched state, every rating."""

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from penguwatch.db.repository import DatabaseRepository

DATA = Path(__file__).parent / "data" / "drivers"
SEED = os.environ.get("PENGUWATCH_SEED_DIR")
LEGACY_IDS = (("simkl_id", "simkl"), ("imdb_id", "imdb"), ("tmdb_id", "tmdb"), ("tvdb_id", "tvdb"))


def load_pre_drivers(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript((DATA / "pre_drivers.sql").read_text(encoding="utf-8"))
    conn.close()


@pytest.fixture(params=["pre_drivers", "seed"])
def legacy_db(request, tmp_path):
    path = tmp_path / "legacy.db"
    if request.param == "seed":
        if not SEED:
            pytest.skip("needs the sanitized seed library (scripts/test.sh --seed)")
        shutil.copyfile(f"{SEED}/penguwatch.db", path)
    else:
        load_pre_drivers(path)
    return path


def _expected_id(value):
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value.strip() if isinstance(value, str) else value


def read_legacy(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    shows = {}
    for r in conn.execute("SELECT * FROM shows"):
        ids = {p: _expected_id(r[c]) for c, p in LEGACY_IDS if r[c] not in (None, "")}
        shows[r["id"]] = {"ids": ids, "title": r["title"], "ratings": (r["simkl_rating"], r["imdb_rating"],
                                                                       r["imdb_votes"], r["user_rating"])}
    episodes = sorted(tuple(r) for r in conn.execute(
        "SELECT show_id, season, number, is_watched, watched_at, title, air_date, simkl_id FROM episodes"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    custom = sorted(tuple(r) for r in conn.execute("SELECT show_id, source_id, value, votes, url FROM title_ratings")) \
        if "title_ratings" in tables else []
    conn.close()
    return shows, episodes, custom


async def test_every_title_keeps_its_ids_episodes_and_ratings(legacy_db):
    shows, episodes, custom = read_legacy(legacy_db)
    assert shows and episodes
    repo = DatabaseRepository(str(legacy_db))
    await repo.initialize()
    try:
        for show_id, want in shows.items():
            got = await repo.get_show_by_id(show_id)
            assert got is not None and got.title == want["title"]
            assert got.ids == want["ids"], show_id
            assert (got.simkl_rating, got.imdb_rating, got.imdb_votes, got.user_rating) == want["ratings"]
            if "simkl" in want["ids"]:  # found again by its library id
                assert (await repo.get_show_by_external_id("simkl", want["ids"]["simkl"])).id == show_id
        listed = {s.id: s.ids for s in await repo.get_shows()}
        assert listed == {i: w["ids"] for i, w in shows.items()}
        db = await repo.read_connection()
        after = sorted(tuple(r) for r in await (await db.execute(
            "SELECT show_id, season, number, is_watched, watched_at, title, air_date, "
            "json_extract(ids_json, '$.simkl') FROM episodes")).fetchall())
        assert after == episodes
        for show_id in {e[0] for e in episodes}:
            for ep in await repo.get_episodes(show_id):
                legacy = next(e for e in episodes if e[:3] == (show_id, ep.season, ep.number))
                assert ep.ids == ({"simkl": legacy[7]} if legacy[7] is not None else {})
        kept = sorted(tuple(r) for r in await (await db.execute(
            "SELECT show_id, source_id, value, votes, url FROM title_ratings")).fetchall())
        assert kept == custom
    finally:
        await repo.close()


# --- the SIMKL login survives the move of its keys (migration 0017) -------------------------------------------------

async def test_the_stored_simkl_login_keeps_working_after_the_migration(tmp_path):
    """pre_drivers.sql stores a login (placeholder tokens) with an expiry and a renewal note under the old simkl_* keys.
    After the migration the app starts with that same login: connected, renewable, nothing to reconnect."""
    from penguwatch.components import Context
    from penguwatch.config import AppConfig
    from penguwatch.drivers.simkl import auth as auth_state
    from penguwatch.sync import component as drivers_component
    from penguwatch.drivers.simkl.client import RENEW_MARGIN

    path = tmp_path / "pre.db"
    load_pre_drivers(path)
    old = dict(sqlite3.connect(path).execute("SELECT key, value FROM app_state WHERE key LIKE 'simkl_%'").fetchall())
    assert old["simkl_access_token"] == "placeholder-access-token"
    repo = DatabaseRepository(str(path))
    await repo.initialize()
    try:
        ctx = Context(app=None, config=AppConfig(), repository=repo)
        component = await drivers_component.register(ctx)
        client = ctx.drivers.get("simkl").client
        try:
            assert client.client_id == old["simkl_client_id"]
            assert client.access_token == old["simkl_access_token"]
            assert client.refresh_token == old["simkl_refresh_token"]
            assert client.token_expires_at == auth_state.ts_from_iso(old["simkl_token_expires_at"])
            login = await auth_state.describe(repo, client, RENEW_MARGIN)
            assert login["auth_state"] == "ok" and login["can_auto_renew"] is True
            assert login["renewal_note"] == old["simkl_renewal_note"]
            assert login["token_renewed_at"] == old["simkl_token_renewed_at"]
            assert await repo.get_state("driver.simkl.scope") == old["simkl_scope"]
            assert await repo.get_state("driver.simkl.last_sync") == old["simkl_last_sync"]
            assert await repo.get_state("driver.simkl.cursor") == old["simkl_activities"]
            assert not [k for k in old if await repo.get_state(k) is not None]  # nothing left under the old names
        finally:
            await component.stop()
    finally:
        await repo.close()


async def test_a_backup_leaves_out_the_drivers_credentials(tmp_path):
    """The backup removes each driver's secret keys, under their new names."""
    from penguwatch.api.exports import SECRET_STATE_KEYS, _strip_secrets

    path = tmp_path / "pre.db"
    load_pre_drivers(path)
    repo = DatabaseRepository(str(path))
    await repo.initialize()
    await repo.close()
    assert {"driver.simkl.access_token", "driver.simkl.refresh_token", "driver.simkl.client_id"} <= set(SECRET_STATE_KEYS)
    _strip_secrets(str(path))
    keys = {k for (k,) in sqlite3.connect(path).execute("SELECT key FROM app_state")}
    assert not keys & set(SECRET_STATE_KEYS) and "driver.simkl.last_sync" in keys
    assert not [k for k in keys if "token" in k and "expires" not in k and "renewed" not in k]
