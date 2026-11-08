"""The settings store's tables (migration 0019, db/settings.py): a 0.15 database migrates, the tables start empty, the
migration runs once, a 0.15 runner still opens a migrated database; rows this build does not know are kept and
ignored; a stored value a newer range refuses costs that value only."""

import logging
import sqlite3

import pytest

from penguwatch.config import AppConfig
from penguwatch.db import migrations
from penguwatch.db.repository import DatabaseRepository
from penguwatch.secrets import SecretStore
from penguwatch.settings.document import SettingsDocument
from penguwatch.settings.hub import SettingsHub
from penguwatch.settings.manager import SettingsManager

SETTINGS_TABLES = {"settings_values", "settings_items", "settings_versions"}


async def make_015_database(path, monkeypatch) -> None:
    """A database at migration 18 (0.15), with a service and a show."""
    with monkeypatch.context() as m:
        m.setattr(migrations, "MIGRATIONS", [x for x in migrations.MIGRATIONS if x[0] <= 18])
        repo = DatabaseRepository(str(path))
        await repo.initialize()
        async with repo.write() as db:
            await db.execute("INSERT INTO services (id, name, monthly_cost) VALUES ('netflix', 'Netflix', 9.5)")
            await db.execute("INSERT INTO shows (title, status) VALUES ('T', 'watching')")
        await repo.close()


def tables(path) -> set:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


async def test_a_015_database_migrates_to_19_and_the_import_writes_revision_1(tmp_path, monkeypatch):
    path = tmp_path / "penguwatch.db"
    await make_015_database(path, monkeypatch)
    assert not SETTINGS_TABLES & tables(path)
    repo = DatabaseRepository(str(path))
    await repo.initialize()
    assert SETTINGS_TABLES <= tables(path)
    db = await repo.read_connection()
    for table in SETTINGS_TABLES:
        assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 0
    assert [tuple(r) for r in await (await db.execute("SELECT id, user_edited FROM services")).fetchall()] == [
        ("netflix", 0)]
    async with repo.write() as conn:  # running the migrations again changes nothing
        assert await migrations.run_migrations(conn) == []
    manager = SettingsManager(AppConfig(), repo, SecretStore(tmp_path / "secrets.json"), SettingsHub(),
                              config_path=str(tmp_path / "config.yaml"), file_raw={"notify": {"hour": 7}})
    await manager.load()
    assert manager.revision == 1 and await repo.settings_initialized()
    await repo.close()
    # a 0.15 runner (no entry 19) opens the migrated database and applies nothing
    with monkeypatch.context() as m:
        m.setattr(migrations, "MIGRATIONS", [x for x in migrations.MIGRATIONS if x[0] <= 18])
        old = DatabaseRepository(str(path))
        await old.initialize()
        assert (await old.get_service("netflix")).name == "Netflix"
        await old.close()


@pytest.fixture
async def manager(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "penguwatch.db"))
    await repo.initialize()
    m = SettingsManager(AppConfig(), repo, SecretStore(tmp_path / "secrets.json"), SettingsHub(),
                        config_path=str(tmp_path / "config.yaml"), file_raw={"optimizer": {"max_active_services": 3}})
    await m.load()
    yield m
    await repo.close()


async def test_rows_a_newer_build_wrote_are_kept_and_ignored(manager, caplog):
    async with manager.repository.write() as db:
        await db.execute("INSERT INTO settings_values (key, value, updated) VALUES ('future.key', '1', 0)")
        await db.execute("INSERT INTO settings_values (key, value, updated) VALUES ('notify.hour', '30', 0)")
    with caplog.at_level(logging.INFO, logger="penguwatch.settings"):
        await manager.load()
    assert "kept and ignored: future.key" in caplog.text
    assert "refused by this build (their defaults are used): notify.hour" in caplog.text
    assert "30" not in caplog.text.split("refused by this build")[1]
    assert manager.config.notify.hour == 9 and manager.values()["notify.hour"] == 9
    await manager.apply(manager.revision, {"optimizer.unused_hours": 1.0}, {})
    _, stored = await manager.repository.load_settings()
    assert stored.values["future.key"] == 1 and stored.values["notify.hour"] == 30  # untouched rows stay
    assert "future.key" not in manager.document.values


async def test_a_version_holds_the_whole_document_and_a_summary(manager):
    applied = await manager.apply(1, {"notify.hour": 7, "server.timezone": "Europe/Berlin"}, {})
    assert applied.summary == "Check every day at 9 → 7; Time zone the server's zone (TZ, else UTC) → Europe/Berlin"
    versions = await manager.versions()
    assert [(v["revision"], v["source"]) for v in versions] == [(2, "save"), (1, "import")]
    doc = await manager.repository.settings_version(2)
    assert doc == SettingsDocument(values={"optimizer.max_active_services": 3, "notify.hour": 7,
                                           "server.timezone": "Europe/Berlin"}, lists={})
    draft = await manager.version(1)
    assert draft == {"revision": 1, "values": {"optimizer.max_active_services": 3, "notify.hour": None,
                                               "server.timezone": None}, "lists": {"webhooks": None}}
