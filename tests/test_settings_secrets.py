"""The Settings tab's secrets (penguwatch.secrets, secrets.json next to the database): written 0600 and atomically,
write-only through the API, never in an export, a version, an answer, the database, the downloadable backup or a log
line; a save whose database write fails puts secrets.json back as it was."""

import json
import logging
import os
import stat
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import secrets as secrets_module
from penguwatch.main import app
from penguwatch.secrets import SecretStore, SecretsFileUnusable, slot_path, slot_updates
from penguwatch.security import reset_rate_limits
from tests.settings_helpers import attach_manager
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

POSIX = sys.platform != "win32"
DIGEST = "https://ntfy.example.com/digest-topic-7f3a91"
HOOK_URL = "https://192.0.2.10/hook-path-c0ffee42"
HOOK_SECRET = "s1gning-secret-9d8e7f6a5b4c"
NEW_HOOK_URL = "https://hooks.example.net/second-hook-b16b00b5"
VALUES = (DIGEST, HOOK_URL, "hook-path-c0ffee42", "digest-topic-7f3a91", HOOK_SECRET, NEW_HOOK_URL, "b16b00b5")


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reset_rate_limits()
    for name in ("PENGUWATCH_NOTIFY_URL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_TZ",
                 "PENGUWATCH_LOG_LEVEL", "PENGUWATCH_MCP_ENABLED", "PENGUWATCH_GRAFANA_URL"):
        monkeypatch.delenv(name, raising=False)


async def _store_with_secrets(tmp_path):
    """A store whose import moved a webhook's address to secrets.json, then a save that set the digest address and
    the webhook's signing secret and added a second webhook with its address."""
    manager = await attach_manager(app.state.container, tmp_path, {"webhooks": [{"name": "Home", "url": HOOK_URL}]})
    async with client() as ac:
        res = await ac.put("/api/settings", json={
            "base_revision": manager.revision, "values": {"optimizer.max_active_services": 2},
            "lists": {"webhooks": [{"id": "home", "name": "Home"}, {"name": "Second"}]},
            "secrets": {"notify.url": DIGEST, "webhook.home.secret": HOOK_SECRET, "webhook.second.url": NEW_HOOK_URL}})
    assert res.status_code == 200, res.text
    stored = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert all(v in stored for v in (DIGEST, HOOK_URL, HOOK_SECRET, NEW_HOOK_URL))  # the positive control
    return manager, res


def _leaks(text) -> list:
    if isinstance(text, bytes):
        return [v for v in VALUES if v.encode() in text]
    return [v for v in VALUES if v in text]


# --- the file -------------------------------------------------------------------------------------------------------

@pytest.mark.skipif(not POSIX, reason="file modes")
def test_the_file_is_written_0600_atomically_and_keeps_unknown_keys(tmp_path):
    path = tmp_path / "data" / "secrets.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "future": {"x": 1}}), encoding="utf-8")
    os.chmod(path, 0o644)
    store = SecretStore(path)
    updates, removals = slot_updates({"notify.url": DIGEST, "webhook.home.url": HOOK_URL})
    store.change(updates, removals)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "future": {"x": 1}, "notify": {"url": DIGEST},
                    "webhooks": {"home": {"url": HOOK_URL}}}
    assert not list(path.parent.glob("*.tmp"))
    store.change(*slot_updates({"webhook.home.url": ""}))  # "" removes; an emptied parent goes too
    assert "webhooks" not in json.loads(path.read_text(encoding="utf-8"))
    assert store.get(("notify", "url")) == DIGEST


def test_slot_paths():
    assert slot_path("notify.url") == ("notify", "url")
    assert slot_path("webhook.home-assistant.url") == ("webhooks", "home-assistant", "url")
    assert slot_path("webhook.home.secret") == ("webhooks", "home", "secret")
    for bad in ("notify.token", "webhook.Home.url", "webhook..url", "webhook.home.other", "webhook.a.b.url", ""):
        assert slot_path(bad) is None, bad
    with pytest.raises(KeyError):
        slot_updates({"nope": "x"})


def test_a_damaged_file_is_never_overwritten(tmp_path, caplog):
    path = tmp_path / "secrets.json"
    path.write_text('{"notify": {"url": "' + DIGEST + '"', encoding="utf-8")  # cut off
    store = SecretStore(path)
    with caplog.at_level(logging.DEBUG):
        assert store.data() == {} and store.problem == "is not valid JSON"
        with pytest.raises(SecretsFileUnusable):
            store.change(*slot_updates({"notify.url": "https://other.example/x"}))
    assert path.read_text(encoding="utf-8").startswith('{"notify"')
    assert not _leaks(caplog.text)


# --- the store, the API, the backups --------------------------------------------------------------------------------

async def test_no_secret_anywhere(setup_test_app, tmp_path):
    """The digest address, two webhook addresses and a signing secret are set; none of them is in the export, any
    version, any settings answer, the database file or the downloadable backup."""
    manager, put = await _store_with_secrets(tmp_path)
    assert not _leaks(put.text)
    async with client() as ac:
        answers = {path: await ac.get(path) for path in (
            "/api/settings", "/api/settings/values", "/api/settings/export", "/api/settings/versions",
            "/api/settings/schema")}
        for version in answers["/api/settings/versions"].json():
            answers[f"version {version['revision']}"] = await ac.get(f"/api/settings/versions/{version['revision']}")
        answers["validate"] = await ac.post("/api/settings/validate", json={"values": {}, "lists": {}})
        answers["backup"] = await ac.get("/api/export/backup.db")
    assert len([k for k in answers if k.startswith("version ")]) == 2
    for name, res in answers.items():
        assert res.status_code == 200, name
        assert not _leaks(res.content), name
    state = answers["/api/settings"].json()
    assert state["secrets"]["notify.url"] == {"set": True, "source": "stored", "host": "ntfy.example.com"}
    assert state["secrets"]["webhook.home.url"] == {"set": True, "source": "stored", "host": "192.0.2.10"}
    assert state["secrets"]["webhook.home.secret"] == {"set": True, "source": "stored", "host": None}
    assert state["secrets"]["webhook.second.url"]["host"] == "hooks.example.net"
    export = answers["/api/settings/export"].text
    assert export.startswith("# PenguWatch settings (Settings > Export)") and "url" not in export.split("\n", 3)[3]
    db = app.state.container.repository.db_path
    for suffix in ("", "-wal"):
        if os.path.exists(db + suffix):
            with open(db + suffix, "rb") as f:
                assert not _leaks(f.read()), suffix
    assert manager.secret_value("notify.url") == DIGEST  # the running parts still get them


async def test_the_api_never_returns_a_secret_even_in_an_error(setup_test_app, tmp_path):
    manager, _ = await _store_with_secrets(tmp_path)
    bad = "ftp://user:" + HOOK_SECRET + "@192.0.2.10/hook"
    async with client() as ac:
        refused = await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {},
                                                      "lists": {}, "secrets": {"webhook.home.url": bad}})
        checked = await ac.post("/api/settings/validate", json={"secrets": {"notify.url": bad,
                                                                            "webhook.home.secret": "short"}})
        typed = await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {
            "notify.hour": HOOK_SECRET}, "lists": {}})
        pasted = await ac.post("/api/settings/validate", json={"lists": {"webhooks": [
            {"id": "home", "name": "Home", "url": NEW_HOOK_URL}]}})
    assert refused.status_code == 422 and refused.json()["errors"][0]["where"] == "secrets.webhook.home.url"
    assert checked.status_code == 200 and not checked.json()["ok"]
    assert typed.status_code == 422 and pasted.status_code == 200 and not pasted.json()["ok"]
    for res in (refused, checked, typed, pasted):
        assert not _leaks(res.text) and "hunter2" not in res.text and "short" not in res.text, res.text


async def test_secrets_never_appear_in_logs(setup_test_app, tmp_path, caplog):
    """Every path of the settings store that touches a secret logs, at DEBUG and up, without its value: the import, a
    save, a refused save, a conflict, a failed applier's ERROR line and the start-up comparison."""
    with caplog.at_level(logging.DEBUG):
        manager, _ = await _store_with_secrets(tmp_path)

        @manager.hub.on(slots=["notify.url", "webhook."], name="test.failing")
        def failing(change):
            raise RuntimeError("could not reach the address")

        async with client() as ac:
            res = await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {}, "lists": {},
                                                      "secrets": {"notify.url": DIGEST + "-v2"}})
            assert res.status_code == 200 and res.json()["live_failed"] == ["notify.url"]
            assert (await ac.put("/api/settings", json={"base_revision": 0, "values": {}, "lists": {},
                                                        "secrets": {"notify.url": DIGEST}})).status_code == 409
            assert (await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {}, "lists": {},
                                                        "secrets": {"webhook.home.url": "ftp://" + HOOK_SECRET}})
                    ).status_code == 422
        await manager.load()  # the start-up path: comparison with config.yaml
    assert "Settings applier test.failing failed: RuntimeError" in caplog.text
    assert caplog.records and not _leaks(caplog.text), _leaks(caplog.text)


async def test_a_failing_appliers_message_never_reaches_the_log(setup_test_app, tmp_path, caplog):
    """An applier's exception can quote the address it could not reach (httpx's HTTPStatusError does)."""
    with caplog.at_level(logging.DEBUG):
        manager, _ = await _store_with_secrets(tmp_path)

        @manager.hub.on(slots=["notify.url"], name="test.leaky")
        def leaky(change):
            raise RuntimeError(f"could not reach {manager.secret_value('notify.url')}")

        await manager.apply(manager.revision, {}, {}, {"notify.url": DIGEST + "-v2"})
    assert not _leaks(caplog.text)


async def test_a_failed_database_write_puts_secrets_json_back(setup_test_app, tmp_path, monkeypatch):
    manager, _ = await _store_with_secrets(tmp_path)
    path = tmp_path / "secrets.json"
    before_bytes, before_data, revision = path.read_bytes(), manager.secrets.snapshot(), manager.revision

    async def broken(*args, **kwargs):
        raise OSError("disk full")

    from penguwatch.db import settings as db_settings
    monkeypatch.setattr(db_settings, "_store_version", broken)
    async with client() as ac:
        res = await ac.put("/api/settings", json={
            "base_revision": revision, "values": {"notify.hour": 7}, "lists": {"webhooks": [{"id": "home",
                                                                                           "name": "Home"}]},
            "secrets": {"notify.url": "https://ntfy.example.com/replaced", "webhook.home.secret": ""}})
        state = (await ac.get("/api/settings")).json()
    assert res.status_code == 500 and res.json()["detail"] == "The settings could not be saved; nothing was changed"
    assert path.read_bytes() == before_bytes  # put back as it was, byte for byte
    assert manager.secrets.data() == before_data and manager.secret_value("notify.url") == DIGEST
    assert manager.revision == revision and state["revision"] == revision
    assert state["values"]["notify.hour"]["value"] == 9 and state["secrets"]["webhook.second.url"]["set"]
    assert state["secrets"]["webhook.home.secret"]["set"]
    if POSIX:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_a_secrets_file_that_cannot_be_written_saves_nothing(setup_test_app, tmp_path, monkeypatch):
    manager, _ = await _store_with_secrets(tmp_path)
    revision = manager.revision

    def refuse(data, path):
        raise PermissionError("read-only")

    monkeypatch.setattr(secrets_module, "write_secrets", refuse)
    async with client() as ac:
        res = await ac.put("/api/settings", json={"base_revision": revision, "values": {"notify.hour": 7},
                                                  "lists": {}, "secrets": {"notify.url": DIGEST + "-x"}})
    assert res.status_code == 500 and res.json()["detail"] == "The secrets file could not be written; nothing was saved"
    assert manager.revision == revision and await manager.repository.settings_revision() == revision
    assert manager.secret_value("notify.url") == DIGEST


async def test_removing_a_webhook_removes_its_slots(setup_test_app, tmp_path):
    manager, _ = await _store_with_secrets(tmp_path)
    async with client() as ac:
        res = await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {},
                                                  "lists": {"webhooks": [{"id": "second", "name": "Second"}]}})
    assert res.status_code == 200
    assert {"webhook.home.url", "webhook.home.secret"} <= set(res.json()["applied"])
    data = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert set(data["webhooks"]) == {"second"} and data["notify"]["url"] == DIGEST
