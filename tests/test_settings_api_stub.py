"""The read routes of the Settings API (api/settings.py, against the settings store): the three
routes answer in their final shapes (api/settings_models.py), never send a URL or a secret, and are guarded like the
rest of /api. (The store's revision starts at 1, at the import; a value is "stored" when a save stored it.)"""

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.api.settings_models import SettingsSchema, SettingsState, SettingsValues
from penguwatch.main import app, security_config
from penguwatch.settings import registry
from tests.settings_helpers import attach_manager
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

HOOK_URL = "https://192.0.2.10/hook-path-token"


def client(**kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kw)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("PENGUWATCH_LOG_LEVEL", "PENGUWATCH_TZ", "PENGUWATCH_MCP_ENABLED", "PENGUWATCH_GRAFANA_URL",
                 "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_NOTIFY_URL", "PENGUWATCH_WEBHOOK_VAR"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
async def store(setup_test_app, clean_env, tmp_path):
    """The settings store on the test container (a config.yaml that sets the optimizer, so nothing else is stored)."""
    return await attach_manager(app.state.container, tmp_path, {"optimizer": {"max_active_services": 3}})


@pytest.mark.asyncio
async def test_the_schema_has_every_setting_with_its_range_and_an_etag(setup_test_app):
    async with client() as ac:
        res = await ac.get("/api/settings/schema")
        assert res.status_code == 200
        schema = SettingsSchema.model_validate(res.json())
        assert [s.key for s in schema.settings] == [s.key for s in registry.SETTINGS]
        assert [s.id for s in schema.sections] == list(registry.SECTION_IDS)
        hour = next(s for s in schema.settings if s.key == "notify.hour")
        assert (hour.min, hour.max, hour.default, hour.env, hour.live) == (0, 23, 9, "PENGUWATCH_NOTIFY_HOUR", True)
        theme = next(s for s in schema.settings if s.key == "display.theme")
        assert "default" in theme.choices and "nord" in theme.choices and theme.choice_labels["nord"] == "Nord"
        assert {d.name: d.offers for d in schema.drivers}["local"] == ["library_sync"]
        assert {r.role: (r.multiple, r.optional) for r in schema.roles} == {
            "library": (False, False), "catalog": (True, False), "schedule": (True, True), "discovery": (True, True)}
        fields = [f.name for f in schema.lists["webhooks"].item_fields]
        assert fields == ["id", "name", "events", "secret_env", "secret_file"] and "url" not in fields
        assert schema.lists["webhooks"].max_items == 10
        assert [s.slot for s in schema.secrets] == ["notify.url", "webhook.{id}.url", "webhook.{id}.secret"]
        etag = res.headers["etag"]
        again = await ac.get("/api/settings/schema", headers={"If-None-Match": etag})
        assert again.status_code == 304


@pytest.mark.asyncio
async def test_settings_report_values_sources_and_about(setup_test_app, clean_env, tmp_path):
    clean_env.setenv("PENGUWATCH_TZ", "Europe/Berlin")
    container = app.state.container
    store = await attach_manager(container, tmp_path, {"optimizer": {"max_active_services": 3}})
    await store.apply(store.revision, {"optimizer.max_active_services": 2}, {})  # stored by a save
    async with client() as ac:
        res = await ac.get("/api/settings")
    assert res.status_code == 200
    state = SettingsState.model_validate(res.json())
    assert state.revision == 2 and set(state.values) == set(registry.BY_KEY)
    assert state.values["optimizer.max_active_services"].model_dump() == {
        "value": 2, "stored": 2, "source": "stored", "pending_restart": False, "live_failed": False,
        "env_ignored": None}
    assert state.values["optimizer.household_priority_boost"].source == "default"
    assert state.values["optimizer.household_priority_boost"].stored is None
    assert (state.values["server.timezone"].value, state.values["server.timezone"].source) == ("Europe/Berlin", "env")
    assert state.values["simkl.app_name"].value == "PenguWatch"
    assert state.values["drivers.catalog"].value == ["simkl"]
    assert state.lists["webhooks"].model_dump() == {"set": False, "items": [], "states": {}}
    assert state.secrets["notify.url"].model_dump() == {"set": False, "source": None, "host": None}
    assert state.about.db_path == container.repository.db_path
    assert state.about.secrets_path.endswith("secrets.json")
    assert state.token_set is False and state.about.token_set is False


@pytest.mark.asyncio
async def test_webhooks_and_secret_slots_never_carry_an_address_or_a_secret(setup_test_app, clean_env, tmp_path):
    clean_env.setenv("PENGUWATCH_NOTIFY_URL", "https://ntfy.example.com/private-topic-name")
    clean_env.setenv("PENGUWATCH_WEBHOOK_VAR", "a-signing-secret-of-enough-length")
    secret_file = tmp_path / "hook_secret"
    secret_file.write_text("another-signing-secret-value", encoding="utf-8")
    # the import keeps only a secret_file under /run/secrets/ (penguwatch.webhooks.check_secret_file); this one is in the
    # test's folder so that its content exists and can be looked for
    from penguwatch.settings import importer
    clean_env.setattr(importer, "check_secret_file", lambda path: None)
    # config.yaml's webhooks, imported (their URLs move to secrets.json)
    await attach_manager(app.state.container, tmp_path, {"webhooks": [
        {"name": "Home", "id": "home", "url": HOOK_URL, "secret_env": "PENGUWATCH_WEBHOOK_VAR"},
        {"name": "Filed", "id": "filed", "url": "https://hooks.example.net/x", "secret_file": str(secret_file)},
        {"name": "Plain", "id": "plain", "url": "https://hooks.example.net/y"},
    ]})
    async with client() as ac:
        body = (await ac.get("/api/settings")).text
        values = (await ac.get("/api/settings/values")).text
    for leaked in ("hook-path-token", "private-topic-name", "a-signing-secret", "another-signing-secret"):
        assert leaked not in body and leaked not in values
    state = SettingsState.model_validate_json(body)
    assert [i["id"] for i in state.lists["webhooks"].items] == ["home", "filed", "plain"]
    assert all("url" not in item for item in state.lists["webhooks"].items)
    assert state.secrets["notify.url"].model_dump() == {"set": True, "source": "env", "host": "ntfy.example.com"}
    assert state.secrets["webhook.home.url"].model_dump() == {"set": True, "source": "stored", "host": "192.0.2.10"}
    assert state.secrets["webhook.home.secret"].model_dump() == {"set": True, "source": "env", "host": None}
    assert state.secrets["webhook.filed.secret"].model_dump() == {"set": True, "source": "file", "host": None}
    assert state.secrets["webhook.plain.secret"].model_dump() == {"set": False, "source": None, "host": None}


@pytest.mark.asyncio
async def test_values_are_the_effective_values(store):
    async with client() as ac:
        res = await ac.get("/api/settings/values")
    values = SettingsValues.model_validate(res.json())
    assert values.revision == 1 and set(values.values) == set(registry.BY_KEY)
    assert values.values["notify.hour"] == 9 and values.values["household.members"] == [
        {"id": "person1", "name": "Me"}, {"id": "person2", "name": "Partner"}]


@pytest.mark.asyncio
async def test_every_settings_route_needs_the_token_when_one_is_set(store, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    async with client() as ac:
        for path in ("/api/settings", "/api/settings/schema", "/api/settings/values"):
            assert (await ac.get(path)).status_code == 401, path
            ok = await ac.get(path, headers={"Authorization": "Bearer s3cret-token"})
            assert ok.status_code == 200, path
        assert (await ac.get("/api/settings", headers={"Authorization": "Bearer s3cret-token"})).json()["token_set"]


@pytest.mark.asyncio
async def test_before_the_app_has_started_the_routes_answer_503():
    had = getattr(app.state, "container", None)
    if had is not None:
        del app.state.container
    try:
        async with client() as ac:
            assert (await ac.get("/api/settings")).status_code == 503
            assert (await ac.get("/api/settings/values")).status_code == 503
    finally:
        if had is not None:
            app.state.container = had
