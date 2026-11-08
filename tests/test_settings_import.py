"""The one-time import of config.yaml into the settings store (settings/importer.py): a 0.15-style file comes across
value for value (the effective config equals what the build before the store ran with, key by key), one bad value
costs that value only, it runs once, and the parts that are not settings (services, rating sources, a file-held login,
the webhooks' addresses) are handed over in the same transaction."""

import copy
import logging
from pathlib import Path

import pytest
import yaml

from penguwatch.config import AppConfig, load_config
from penguwatch.db.repository import DatabaseRepository
from penguwatch.db.services import same_refreshable
from penguwatch.drivers.base import state_key
from penguwatch.models import ServiceConfig
from penguwatch.secrets import SecretStore
from penguwatch.settings import legacy, registry
from penguwatch.settings.hub import SettingsHub
from penguwatch.settings.importer import get_path, jsonable, present, refresh_catalogue
from penguwatch.settings.manager import SettingsManager

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
# the 0.15 example's services list, which a 0.15 user's config.yaml still has: it moved to the catalogue
OLD_SERVICES = yaml.safe_load((ROOT / "src" / "penguwatch" / "catalog" / "services.yaml").read_text(
    encoding="utf-8"))["services"]
HOOK_URL = "https://192.0.2.10/api/webhook/made-up-hook-id-123"


def owner_shaped() -> dict:
    """A made-up config.yaml in the shape of the owner's (every section the live file has) plus the sections a public
    0.15 user may have: drivers, a driver's options, display, webhooks, rating sources."""
    services = copy.deepcopy(OLD_SERVICES)
    by_id = {s["id"]: s for s in services}
    by_id["hidive"]["notes"] = "Annual plan, my own note"  # notes are not refreshed: still untouched
    by_id["netflix"]["aliases"] = list(reversed(by_id["netflix"]["aliases"]))  # same set: untouched
    by_id["max"]["aliases"] = by_id["max"]["aliases"] + ["Max Original"]  # the user's own alias: edited
    services.append({"id": "local_tv", "name": "Local TV", "monthly_cost": 0.0, "is_active": True,
                     "aliases": ["Antenna"]})  # not in the catalogue: edited
    return {
        "server": {"host": "0.0.0.0", "port": 8080, "log_level": "info", "timezone": "America/New_York"},
        "database": {"db_path": "data/penguwatch.db"},
        "simkl": {"client_id": "", "access_token": "", "auto_sync_interval_minutes": 30, "idle_after_hours": 12,
                  "idle_sync_interval_minutes": 180, "app_name": "PenguWatch"},
        "household": {"members": ["Alex", "Sam"]},
        "optimizer": {"max_active_services": 3, "household_priority_boost": 2.0, "ready_to_binge_boost": 1.5},
        "services": services,
        "mcp": {"enabled": True},
        "drivers": {"library": "simkl", "catalog": ["simkl", "tvmaze"], "schedule": ["tvmaze"]},
        "tvmaze": {"timeout_seconds": 15},
        "display": {"theme": "nord"},
        "webhooks": [{"name": "Home Assistant", "url": HOOK_URL, "events": ["title.added"],
                      "secret_env": "PENGUWATCH_WEBHOOK_HA"}],
        "ratings": {"sources": [{"id": "letterboxd", "name": "Letterboxd", "label": "LB", "scale_min": 0,
                                 "scale_max": 5}]},
    }


def old_build(raw: dict, path) -> dict:
    """What the build before the settings store ran with, from the file read strictly (it is valid) and the old
    defaults for what it leaves out (penguwatch.settings.legacy)."""
    strict = AppConfig(**raw)
    old = legacy.old_values(legacy.ran_on_example(path))
    return {s.key: jsonable(get_path(strict, s.key)) if present(raw, s.key) else old[s.key]
            for s in registry.SETTINGS}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("PENGUWATCH_NOTIFY_URL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_TZ",
                 "PENGUWATCH_LOG_LEVEL", "PENGUWATCH_MCP_ENABLED", "PENGUWATCH_GRAFANA_URL", "SIMKL_CLIENT_ID",
                 "SIMKL_ACCESS_TOKEN", "PENGUWATCH_CONFIG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "data" / "penguwatch.db"))
    await r.initialize()
    yield r
    await r.close()


async def run(repo, tmp_path, raw, config_path="config.yaml", config=None) -> SettingsManager:
    path = None if config_path is None else str(tmp_path / config_path)
    manager = SettingsManager(config or AppConfig(), repo, SecretStore(tmp_path / "data" / "secrets.json"),
                              SettingsHub(), config_path=path, file_raw=raw)
    await manager.load()
    await refresh_catalogue(repo)
    return manager


def assert_same_as_the_old_build(manager: SettingsManager, raw: dict, path) -> None:
    old = old_build(raw, path)
    new = manager.values()
    assert {k: new[k] for k in old} == old
    live = {s.key: jsonable(get_path(manager.config, s.key)) for s in registry.SETTINGS}
    assert live == old  # the live config the app runs on (every key, at the start)


# --- value for value ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("config_path", ["config.example.yaml", None])
async def test_the_example_config_imports_to_the_same_effective_values(repo, tmp_path, config_path):
    manager = await run(repo, tmp_path, copy.deepcopy(EXAMPLE), config_path)
    path = None if config_path is None else str(tmp_path / config_path)
    assert_same_as_the_old_build(manager, EXAMPLE, path)
    assert manager.import_report() == [] and manager.revision == 1
    # the example has no services list now: the catalogue (the old list) seeds the empty table, as the old seed did
    table = {s.id: s for s in await repo.get_services()}
    assert {s["id"] for s in OLD_SERVICES} == set(table)
    for row in OLD_SERVICES:
        assert table[row["id"]] == ServiceConfig(**row)


async def test_an_owner_shaped_config_imports_to_the_same_effective_values(repo, tmp_path, monkeypatch, caplog):
    raw = owner_shaped()
    with caplog.at_level(logging.INFO, logger="penguwatch.settings"):
        manager = await run(repo, tmp_path, copy.deepcopy(raw))
    assert_same_as_the_old_build(manager, raw, str(tmp_path / "config.yaml"))
    assert manager.import_report() == []
    # rule 2: the file sets the optimizer, so nothing is added; every key it sets is stored, even at its default
    assert set(manager.document.values) == {s.key for s in registry.SETTINGS if present(raw, s.key)}
    assert len(manager.document.values) == 16
    # the webhooks: the same hooks, the address now in secrets.json
    old_hooks = [h.model_dump() for h in AppConfig(**raw).webhooks]
    assert [h.model_dump() for h in manager.config.webhooks] == old_hooks
    assert manager.document.lists["webhooks"][0]["id"] == "home-assistant"
    assert manager.stored_secret("webhook.home-assistant.url") == HOOK_URL
    assert HOOK_URL not in str(manager.document.to_json())
    # the services: what the old seed wrote, the refreshed ones equal to it as a person reads them
    table = {s.id: s for s in await repo.get_services()}
    for row in raw["services"]:
        want = ServiceConfig(**row)
        got = table[row["id"]]
        assert same_refreshable(got, want) and got.model_dump(exclude={"name", "aliases", "titles", "includes"}) == \
            want.model_dump(exclude={"name", "aliases", "titles", "includes"}), row["id"]
    # the rating source: editable now
    sources = {s.id: s for s in await repo.get_rating_sources()}
    assert sources["letterboxd"].origin == "api" and not sources["letterboxd"].read_only
    summary = (await repo.settings_versions())[0]["summary"]
    assert summary == "Imported from config.yaml: 16 values; webhooks 1; services 15"
    assert "Imported settings from config.yaml: 16 values; webhooks 1; services 15" in caplog.text
    assert HOOK_URL not in caplog.text


async def test_a_file_without_the_optimizer_keeps_one_service_at_once(repo, tmp_path):
    manager = await run(repo, tmp_path, {"household": {"members": ["Me"]}})
    assert manager.document.values == {"household.members": ["Me"], "optimizer.max_active_services": 1}
    assert manager.config.optimizer.max_active_services == 1


async def test_a_file_less_install_stores_nothing(repo, tmp_path):
    manager = await run(repo, tmp_path, {}, config_path=None)
    assert manager.document.values == {} and manager.config.optimizer.max_active_services == 3
    assert (await repo.settings_versions())[0]["summary"].startswith("Started with the defaults") or \
        (await repo.settings_versions())[0]["summary"].startswith("Imported from config.yaml: 0 values")


async def test_a_bad_value_is_skipped_and_reported_and_the_start_goes_on(repo, tmp_path, caplog):
    raw = {"optimizer": {"max_active_services": 99, "household_priority_boost": 4},
           "server": {"timezone": "Mars/Olympus_Mons-77"}, "drivers": {"library": "nosuch"}}
    with caplog.at_level(logging.INFO):
        manager = await run(repo, tmp_path, raw)
    report = {r["key"]: r["reason"] for r in manager.import_report()}
    assert set(report) == {"optimizer.max_active_services", "server.timezone", "drivers.library"}
    assert manager.document.values == {"optimizer.household_priority_boost": 4.0}
    assert manager.config.optimizer.max_active_services == 3  # the default (the file set it, wrongly)
    assert "99" not in str(report) and "Mars" not in str(report) and "Mars" not in caplog.text
    assert "3 left out (defaults kept)" in caplog.text


async def test_an_imported_hook_signing_with_another_variable_or_file_is_left_out(repo, tmp_path):
    """The import keeps a secret_env / secret_file only when penguwatch.webhooks.check_secret_env / check_secret_file
    accept it (a PENGUWATCH_WEBHOOK_* variable, a file under /run/secrets/); without it the hook would send unsigned,
    so the whole hook is left out, its address is not moved, and the report names the field, never the value."""
    raw = {"webhooks": [
        {"name": "Ok", "url": "https://192.0.2.10/ok", "secret_env": "PENGUWATCH_WEBHOOK_OK"},
        {"name": "Token", "url": "https://192.0.2.11/token-77", "secret_env": "PENGUWATCH_ACCESS_TOKEN"},
        {"name": "Shadow", "url": "https://192.0.2.12/shadow-77", "secret_file": "/etc/shadow"},
        {"name": "Filed", "url": "https://192.0.2.13/filed", "secret_file": "/run/secrets/filed"}]}
    manager = await run(repo, tmp_path, raw)
    report = {r["key"]: r["reason"] for r in manager.import_report()}
    assert set(report) == {"webhooks[1].secret_env", "webhooks[2].secret_file"}
    assert "ACCESS_TOKEN" not in str(report) and "shadow" not in str(report)
    assert [i["id"] for i in manager.document.lists["webhooks"]] == ["ok", "filed"]
    assert manager.stored_secret("webhook.token.url") is None and manager.stored_secret("webhook.shadow.url") is None


async def test_the_import_runs_once(repo, tmp_path):
    first = await run(repo, tmp_path, {"notify": {"hour": 6}})
    await first.apply(first.revision, {"notify.hour": 8}, {})
    again = await run(repo, tmp_path, {"notify": {"hour": 5}, "mcp": {"enabled": True}})
    assert again.revision == 2 and again.document.values == {"notify.hour": 8, "optimizer.max_active_services": 1}
    assert again.config_yaml_state()["differing_keys"] == 2  # notify.hour, mcp.enabled
    assert len(await repo.settings_versions()) == 2


async def test_a_failed_import_rolls_back_and_runs_again_next_start(repo, tmp_path, monkeypatch):
    from penguwatch.db import ratings as db_ratings

    async def broken(db, specs):
        raise RuntimeError("boom")

    monkeypatch.setattr(db_ratings, "import_rating_sources", broken)
    with pytest.raises(RuntimeError):
        await run(repo, tmp_path, {"notify": {"hour": 6}, "services": OLD_SERVICES[:2]})
    assert not await repo.settings_initialized() and await repo.get_services() == []
    monkeypatch.undo()
    manager = await run(repo, tmp_path, {"notify": {"hour": 6}, "services": OLD_SERVICES[:2]})
    assert manager.revision == 1 and len(await repo.get_services()) == 2


async def test_a_file_held_login_moves_only_into_an_empty_stored_login(repo, tmp_path, caplog):
    await repo.set_state(state_key("simkl", "access_token"), "stored-token-abc")
    raw = {"simkl": {"client_id": "file-client-id-123", "access_token": "file-token-xyz"}}
    with caplog.at_level(logging.INFO):
        manager = await run(repo, tmp_path, raw)
    assert await repo.get_state(state_key("simkl", "client_id")) == "file-client-id-123"
    assert await repo.get_state(state_key("simkl", "access_token")) == "stored-token-abc"
    assert "simkl.client_id: moved to the stored login" in caplog.text
    assert "simkl.access_token: the stored login is kept" in caplog.text
    assert "file-client-id-123" not in caplog.text and "file-token-xyz" not in caplog.text
    assert "simkl.client_id" not in manager.document.values


async def test_rating_sources_become_editable_and_a_scale_conflict_is_reported(repo, tmp_path):
    from penguwatch.ratings import RatingSourceSpec
    await repo.seed_rating_sources([RatingSourceSpec(id="mine", name="Mine", label="M", scale_min=0, scale_max=10)])
    async with repo.write() as db:
        await db.execute("INSERT INTO shows (title, status) VALUES ('T', 'watching')")
        await db.execute("INSERT INTO title_ratings (show_id, source_id, value) VALUES (1, 'mine', 8)")
    raw = {"ratings": {"sources": [{"id": "mine", "name": "Mine", "label": "M", "scale_min": 0, "scale_max": 5},
                                   {"id": "other", "name": "Other", "label": "O", "scale_min": 1, "scale_max": 4}]}}
    manager = await run(repo, tmp_path, raw)
    sources = {s.id: s for s in await repo.get_rating_sources()}
    assert sources["mine"].scale_max == 10 and sources["mine"].origin == "api"  # left as it was, now editable
    assert sources["other"].origin == "api"
    assert [r["key"] for r in manager.import_report()] == ["ratings.sources[0]"]


async def test_the_differs_line_names_keys_and_never_values(repo, tmp_path, caplog):
    await run(repo, tmp_path, {"optimizer": {"max_active_services": 2}, "webhooks": [{"name": "a", "url": HOOK_URL}]})
    with caplog.at_level(logging.INFO, logger="penguwatch.settings"):
        manager = await run(repo, tmp_path, {"optimizer": {"max_active_services": 4}, "mcp": {"enabled": True},
                                             "webhooks": [{"name": "a", "url": HOOK_URL + "-changed"}],
                                             "services": [{**OLD_SERVICES[0], "aliases": ["Changed"]}]})
    line = next(r.getMessage() for r in caplog.records if "config.yaml is ignored" in r.getMessage())
    assert "3 keys differ: mcp.enabled, optimizer.max_active_services, webhooks" in line
    assert "1 service differs from the Services tab" in line
    assert HOOK_URL not in caplog.text and " 4" not in line
    assert manager.config_yaml_state()["differing_keys"] == 3 and manager.config_yaml_state()["differing_services"] == 1


# --- load_config: strict for the bootstrap keys, lenient for the rest --------------------------------------------------

def test_load_config_is_strict_for_the_bootstrap_keys_and_lenient_for_the_rest(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("server: {port: 8090, log_level: loud}\noptimizer: {max_active_services: 0}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="penguwatch.config"):
        config = load_config(str(path))
    assert config.server.port == 8090 and config.server.log_level == "info"
    assert config.optimizer.max_active_services == 3
    assert "server.log_level left out" in caplog.text and "optimizer.max_active_services left out" in caplog.text
    assert "loud" not in caplog.text
    for bad in ("server: {port: not-a-port}\n", "database: {db_path: [1, 2]}\n", "server: 5\n"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError) as refused:
            load_config(str(path))
        assert "not-a-port" not in str(refused.value)
