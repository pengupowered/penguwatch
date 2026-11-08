"""Environment variables over the settings store: a variable that pins a setting or a secret slot wins, the page
reports it ("From environment"), and a save that touches it is refused with 400 naming the variable, leaving the stored
row as it was. A variable its setting's field refuses is ignored: the setting stays editable and
the reason is reported next to it, never the value."""

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app
from penguwatch.security import reset_rate_limits
from tests.settings_helpers import attach_manager
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

PINNING = ("PENGUWATCH_NOTIFY_URL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_TZ",
           "PENGUWATCH_LOG_LEVEL", "PENGUWATCH_MCP_ENABLED", "PENGUWATCH_GRAFANA_URL", "PENGUWATCH_WEBHOOK_HOME")


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    reset_rate_limits()
    for name in PINNING:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


async def test_pinned_key_refuses_a_save_and_keeps_its_row(setup_test_app, tmp_path, env):
    env.setenv("PENGUWATCH_TZ", "Europe/Berlin")
    manager = await attach_manager(app.state.container, tmp_path, {"server": {"timezone": "Asia/Tokyo"}})
    revision = manager.revision
    async with client() as ac:
        refused = await ac.put("/api/settings", json={"base_revision": revision,
                                                      "values": {"server.timezone": "America/New_York",
                                                                 "notify.hour": 7}, "lists": {}})
        reset = await ac.put("/api/settings", json={"base_revision": revision, "values": {"server.timezone": None},
                                                    "lists": {}})
        state = (await ac.get("/api/settings")).json()
    assert refused.status_code == 400 and reset.status_code == 400
    body = refused.json()
    assert body["detail"] == "Set by the environment"
    assert body["errors"] == [{"where": "values.server.timezone",
                               "message": "PENGUWATCH_TZ sets this; remove it and restart to edit it here."}]
    assert manager.revision == revision and await manager.repository.settings_revision() == revision
    assert manager.document.values["server.timezone"] == "Asia/Tokyo"  # the stored row is unchanged
    assert state["values"]["server.timezone"] == {"value": "Europe/Berlin", "stored": "Asia/Tokyo", "source": "env",
                                                  "pending_restart": False, "live_failed": False, "env_ignored": None}
    assert state["values"]["notify.hour"]["value"] == 9  # nothing of the refused save was applied
    assert app.state.container.config.server.timezone == "Europe/Berlin"


async def test_a_pinned_digest_address_refuses_a_write(setup_test_app, tmp_path, env):
    env.setenv("PENGUWATCH_NOTIFY_URL", "https://ntfy.example.com/from-env-topic")
    manager = await attach_manager(app.state.container, tmp_path, {})
    async with client() as ac:
        refused = await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {}, "lists": {},
                                                      "secrets": {"notify.url": "https://ntfy.example.com/other"}})
        state = (await ac.get("/api/settings")).json()
    assert refused.status_code == 400
    assert refused.json()["errors"] == [{"where": "secrets.notify.url", "message":
                                         "PENGUWATCH_NOTIFY_URL sets this; remove it and restart to edit it here."}]
    assert manager.stored_secret("notify.url") is None
    assert state["secrets"]["notify.url"] == {"set": True, "source": "env", "host": "ntfy.example.com"}
    assert "from-env-topic" not in str(state)
    assert manager.secret_value("notify.url") == "https://ntfy.example.com/from-env-topic"


async def test_a_webhook_secret_read_from_a_variable_or_a_file_refuses_a_stored_one(setup_test_app, tmp_path, env):
    env.setenv("PENGUWATCH_WEBHOOK_HOME", "x" * 20)
    manager = await attach_manager(app.state.container, tmp_path, {"webhooks": [
        {"name": "Home", "url": "https://192.0.2.10/a", "secret_env": "PENGUWATCH_WEBHOOK_HOME"},
        {"name": "Filed", "url": "https://192.0.2.11/b", "secret_file": "/run/secrets/filed"}]})
    async with client() as ac:
        res = await ac.post("/api/settings/validate", json={"secrets": {
            "webhook.home.secret": "y" * 20, "webhook.filed.secret": "z" * 20}})
        state = (await ac.get("/api/settings")).json()
    errors = {e["where"]: e["message"] for e in res.json()["errors"]}
    assert errors == {"secrets.webhook.home.secret": "The webhook reads its signing secret from "
                                                     "PENGUWATCH_WEBHOOK_HOME; change it there.",
                      "secrets.webhook.filed.secret": "The webhook reads its signing secret from a file."}
    assert state["secrets"]["webhook.home.secret"] == {"set": True, "source": "env", "host": None}
    assert state["secrets"]["webhook.filed.secret"] == {"set": False, "source": "file", "host": None}
    assert state["lists"]["webhooks"]["states"]["filed"]["state"] == "disabled"


async def test_an_invalid_pinning_variable_is_ignored_and_reported(setup_test_app, tmp_path, env):
    env.setenv("PENGUWATCH_NOTIFY_HOUR", "25")
    env.setenv("PENGUWATCH_TZ", "Mars/Olympus_Mons")
    manager = await attach_manager(app.state.container, tmp_path, {"notify": {"hour": 7}})
    async with client() as ac:
        state = (await ac.get("/api/settings")).json()
        saved = await ac.put("/api/settings", json={"base_revision": manager.revision, "values": {"notify.hour": 6},
                                                    "lists": {}})
    hour = state["values"]["notify.hour"]
    assert (hour["value"], hour["source"]) == (7, "stored")  # the stored value applies, and stays editable
    assert hour["env_ignored"].startswith("PENGUWATCH_NOTIFY_HOUR is set but not valid") and "25" not in hour[
        "env_ignored"]
    assert state["values"]["server.timezone"]["env_ignored"].startswith("PENGUWATCH_TZ is set but not valid")
    assert "Mars" not in state["values"]["server.timezone"]["env_ignored"]
    assert saved.status_code == 200 and app.state.container.config.notify.hour == 6


async def test_a_restored_version_leaves_a_pinned_key_alone(setup_test_app, tmp_path, env):
    manager = await attach_manager(app.state.container, tmp_path, {"server": {"log_level": "warning"}})
    await manager.apply(manager.revision, {"server.log_level": "error"}, {})
    env.setenv("PENGUWATCH_LOG_LEVEL", "debug")
    manager.env = type(manager.env)()  # the next start reads the variable
    draft = await manager.version(1)
    assert "server.log_level" not in draft["values"]
