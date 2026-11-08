"""`penguwatch settings check-import CONFIG DB`: a dry run of the one-time import on a private copy of the database.
It writes nothing to the database it is given (nor next to it), builds no driver and opens no network connection
(the copy holds the owner's tracker login and outbox), and prints key names and counts, never a value."""

import hashlib
import json
import socket
from pathlib import Path

import pytest
import yaml

from penguwatch import cli
from penguwatch.db import migrations
from penguwatch.db.repository import DatabaseRepository
from penguwatch.settings.check import check_import

ROOT = Path(__file__).resolve().parent.parent
HOOK_URL = "https://192.0.2.10/hook-made-up-77aa"
LOGIN = "stored-token-made-up-5c5c"
# the 0.15 example's services list (a 0.15 user's config.yaml still has it); it moved to the catalogue
OLD_SERVICES = yaml.safe_load((ROOT / "src" / "penguwatch" / "catalog" / "services.yaml").read_text(
    encoding="utf-8"))["services"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
async def backup(tmp_path, monkeypatch) -> Path:
    """A 0.15 database (migration 18) with a tracker login, a service and a title, closed (as a backup copy is)."""
    folder = tmp_path / "backup"
    folder.mkdir()
    path = folder / "penguwatch.db"
    with monkeypatch.context() as m:
        m.setattr(migrations, "MIGRATIONS", [x for x in migrations.MIGRATIONS if x[0] <= 18])
        repo = DatabaseRepository(str(path))
        await repo.initialize()
        async with repo.write() as db:  # what 0.15's start-up seed wrote
            for s in OLD_SERVICES:
                await db.execute(
                    "INSERT INTO services (id, name, monthly_cost, is_active, aliases_json, includes_json, titles_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (s["id"], s["name"], s.get("monthly_cost", 0), 0,
                                                    json.dumps(s.get("aliases", [])), json.dumps(s.get("includes", [])),
                                                    json.dumps(s.get("titles", []))))
            await db.execute("INSERT INTO app_state (key, value) VALUES ('driver.simkl.access_token', ?)", (LOGIN,))
            await db.execute("INSERT INTO shows (title, status) VALUES ('T', 'watching')")
        await repo.close()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(path) + suffix).exists()
    return path


def write_config(tmp_path, extra=None) -> Path:
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["services"] = OLD_SERVICES  # a config.yaml copied from the 0.15 example
    data.update({"server": {"timezone": "America/New_York", "log_level": "info"}, "mcp": {"enabled": True},
                 "webhooks": [{"name": "Home", "url": HOOK_URL, "secret_env": "PENGUWATCH_WEBHOOK_HOME"}]})
    data.update(extra or {})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to build a driver or to open a connection fails the test."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("check-import must not open a connection or build a driver")

    async def refuse_async(*args, **kwargs):
        refuse(*args)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    import penguwatch.sync.active as active
    monkeypatch.setattr(active, "build_drivers", refuse_async)
    for name in ("SIMKL_ACCESS_TOKEN", "SIMKL_CLIENT_ID", "PENGUWATCH_TZ", "PENGUWATCH_MCP_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_check_import_writes_nothing_and_makes_no_network_call(backup, tmp_path, no_network):
    config = write_config(tmp_path)
    before, config_before = digest(backup), digest(config)
    files_before = sorted(p.name for p in backup.parent.iterdir())
    lines = []
    code = check_import(str(config), str(backup), out=lines.append)
    text = "\n".join(lines)
    assert code == 0, text
    assert no_network == []
    assert digest(backup) == before and digest(config) == config_before
    assert sorted(p.name for p in backup.parent.iterdir()) == files_before  # no -wal, -shm or secrets.json
    assert "migration: 18 -> 19" in text
    assert "  server.timezone: same" in text and "  mcp.enabled: same" in text
    assert "webhooks: 1 in config.yaml, 1 stored, 1 same" in text
    assert "services: 14 in config.yaml, 14 same" in text
    assert "result: every value came across unchanged" in text
    for value in (HOOK_URL, "192.0.2.10", LOGIN, "America/New_York", "PENGUWATCH_WEBHOOK_HOME"):
        assert value not in text


def test_the_command_runs_it_and_reports_a_skipped_key(backup, tmp_path, no_network, capsys):
    config = write_config(tmp_path, {"optimizer": {"max_active_services": 99}})
    before = digest(backup)
    assert cli.main(["settings", "check-import", str(config), str(backup)]) == 1
    out = capsys.readouterr().out
    assert "optimizer.max_active_services: skipped (" in out and "99" not in out
    assert digest(backup) == before and no_network == []
    assert cli.main(["settings", "check-import", "--config", str(config), "--db", str(backup)]) == 1


def test_it_cannot_run_without_its_files(tmp_path, capsys):
    assert check_import(str(tmp_path / "none.yaml"), str(tmp_path / "none.db"), out=print) == 2
    config = write_config(tmp_path)
    assert check_import(str(config), str(tmp_path / "none.db"), out=print) == 2
    assert cli.main(["settings", "check-import"]) == 2


def test_live_needs_an_imported_copy(backup, tmp_path, no_network):
    lines = []
    assert check_import(str(write_config(tmp_path)), str(backup), live=True, out=lines.append) == 2
    assert "no imported settings yet" in "\n".join(lines)
