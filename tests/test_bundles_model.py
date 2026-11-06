"""Bundle membership: model validation, migration 0004, row mapping and seeding."""

import sqlite3

import pytest
from pydantic import ValidationError

from penguwatch.db.migrations import MIGRATIONS
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "bundles.db"))
    yield r
    await r.close()


def _bundle(**kw):
    base = dict(id="combo", name="Combo", monthly_cost=19.99, includes=["hulu", "disney_plus"])
    base.update(kw)
    return ServiceConfig(**base)


def test_model_defaults_and_is_bundle():
    plain = ServiceConfig(id="x", name="X")
    assert plain.includes == []
    assert plain.is_bundle is False
    assert _bundle().is_bundle is True
    assert "is_bundle" not in _bundle().model_dump()


def test_model_rejects_self_inclusion_and_duplicates():
    with pytest.raises(ValidationError):
        _bundle(includes=["combo"])
    with pytest.raises(ValidationError):
        _bundle(includes=["hulu", "hulu"])


def test_model_rejects_bad_ids_and_bounds():
    with pytest.raises(ValidationError):
        _bundle(includes=["not valid!"])
    with pytest.raises(ValidationError):
        _bundle(includes=[f"s{i}" for i in range(13)])
    with pytest.raises(ValidationError):
        ServiceConfig(id="x", name="X", aliases=["a"] * 41)
    with pytest.raises(ValidationError):
        ServiceConfig(id="x", name="X", aliases=["a" * 61])
    assert ServiceConfig(id="x", name="X", aliases=["a" * 60] * 40).aliases


async def test_bundle_round_trip(repo):
    await repo.initialize()
    await repo.upsert_service(_bundle())
    got = await repo.get_service("combo")
    assert got.includes == ["hulu", "disney_plus"]
    assert got.is_bundle


async def test_fresh_db_reaches_latest_version(repo):
    await repo.initialize()
    db = await repo.get_connection()
    cur = await db.execute("SELECT MAX(version) FROM schema_migrations")
    assert (await cur.fetchone())[0] == len(MIGRATIONS)


async def test_legacy_db_gains_column(tmp_path):
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE services (id TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_cost REAL DEFAULT 0.0,"
        " is_active INTEGER DEFAULT 0, renewal_day INTEGER, renewal_date TEXT,"
        " aliases_json TEXT DEFAULT '[]', notes TEXT);"
        "INSERT INTO services (id, name, monthly_cost, is_active) VALUES ('old', 'Old', 5.0, 1);"
    )
    raw.commit()
    raw.close()
    r = DatabaseRepository(path)
    try:
        await r.initialize()
        svc = await r.get_service("old")
        assert svc.includes == [] and svc.is_active and svc.monthly_cost == 5.0
    finally:
        await r.close()


async def test_seeding_twice_preserves_user_edits_but_updates_includes_and_name(repo):
    await repo.initialize([ServiceConfig(id="combo", name="Combo", monthly_cost=10.0, includes=["a"],
                                         aliases=["One"])])
    await repo.upsert_service(ServiceConfig(id="combo", name="Combo", monthly_cost=12.5, is_active=True,
                                            renewal_day=7, includes=["a"], aliases=["One"],
                                            notes="mine"))
    await repo.initialize([ServiceConfig(id="combo", name="Combo v2", monthly_cost=10.0,
                                         includes=["a", "b"], aliases=["Two"], notes="config")])
    got = await repo.get_service("combo")
    assert got.monthly_cost == 12.5
    assert got.is_active is True
    assert got.renewal_day == 7
    assert got.notes == "mine"
    assert got.name == "Combo v2"
    assert got.aliases == ["Two"]
    assert got.includes == ["a", "b"]


def test_row_mapping_tolerates_missing_includes_column():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 'x' AS id, 'X' AS name, 1.0 AS monthly_cost, 0 AS is_active, NULL AS renewal_day,"
        " NULL AS renewal_date, '[]' AS aliases_json, NULL AS notes"
    ).fetchone()
    assert DatabaseRepository(":memory:")._row_to_service(row).includes == []
    row = conn.execute(
        "SELECT 'x' AS id, 'X' AS name, 1.0 AS monthly_cost, 0 AS is_active, NULL AS renewal_day,"
        " NULL AS renewal_date, '[]' AS aliases_json, NULL AS notes, NULL AS includes_json"
    ).fetchone()
    assert DatabaseRepository(":memory:")._row_to_service(row).includes == []
