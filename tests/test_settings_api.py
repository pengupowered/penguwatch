"""The Settings API's writing routes (api/settings.py): validate, save, conflicts, versions and restore, export and
import, the guard on every route, and errors that never echo the input."""

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from penguwatch.api.settings_models import ImportDraft, PutResponse, SettingsState, ValidateResponse, VersionInfo
from penguwatch.db.settings import MAX_VERSIONS
from penguwatch.main import app, security_config
from penguwatch.security import reset_rate_limits
from penguwatch.settings import registry
from tests.settings_helpers import attach_manager
from tests.test_api import setup_test_app  # noqa: F401  (fixture)


def client(**kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kw)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reset_rate_limits()
    for name in ("PENGUWATCH_NOTIFY_URL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_TZ",
                 "PENGUWATCH_LOG_LEVEL", "PENGUWATCH_MCP_ENABLED", "PENGUWATCH_GRAFANA_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def store(setup_test_app, tmp_path):
    return await attach_manager(app.state.container, tmp_path, {"optimizer": {"max_active_services": 3},
                                                                "household": {"members": ["Ann", "Bo"]}})


async def put(ac, base, values=None, lists=None, secrets=None, source=None):
    url = "/api/settings" + (f"?source={source}" if source else "")
    return await ac.put(url, json={"base_revision": base, "values": values or {}, "lists": lists or {},
                                   "secrets": secrets or {}})


async def test_a_save_applies_at_once_and_says_what_changed(store):
    container = app.state.container
    async with client() as ac:
        res = await put(ac, 1, {"optimizer.max_active_services": 2, "notify.hour": 7, "drivers.schedule": ["tvmaze"]})
        assert res.status_code == 200, res.text
        body = PutResponse.model_validate(res.json())
        state = SettingsState.model_validate((await ac.get("/api/settings")).json())
    assert body.revision == 2 and body.reload is False
    assert body.summary.startswith("Services kept at once 3 → 2; Check every day at 9 → 7")
    assert body.applied == ["drivers.schedule", "notify.hour", "optimizer.max_active_services"]
    assert body.restart_needed == ["drivers.schedule"] and body.live_failed == []
    assert container.config.optimizer.max_active_services == 2 and container.config.notify.hour == 7
    assert container.config.drivers.schedule == ["simkl"]  # live: False keeps the running value
    assert state.values["drivers.schedule"].value == ["tvmaze"] and state.values["drivers.schedule"].pending_restart
    assert state.values["optimizer.max_active_services"].source == "stored"


async def test_resetting_a_key_and_the_household_reload(store):
    async with client() as ac:
        res = await put(ac, 1, {"optimizer.max_active_services": None, "household.members": ["Ann"]})
    body = res.json()
    assert res.status_code == 200 and body["reload"] is True
    assert "Services kept at once reset" in body["summary"]
    assert "optimizer.max_active_services" not in store.document.values
    assert app.state.container.config.household.members == ["Ann"]


async def test_refused_values_are_placed_and_never_echoed(store):
    async with client() as ac:
        res = await put(ac, 1, {"notify.hour": 25, "optimizer.household_priority_boost": "lots-of-it-77",
                                "display.theme": "no-such-theme-31", "no.such": 1})
        rule = await put(ac, 1, {"simkl.idle_sync_interval_minutes": 20})
        checked = await ac.post("/api/settings/validate", json={"values": {"notify.hour": 25}})
    assert res.status_code == 422 and res.json()["detail"] == "Some values were refused"
    where = {e["where"]: e["message"] for e in res.json()["errors"]}
    assert where["values.no.such"] == "No such setting"
    assert where["values.notify.hour"] == "Enter at most 23"
    assert where["values.optimizer.household_priority_boost"] == "Enter a number"
    assert where["values.display.theme"] == "Pick one of the built-in themes"
    # the rule across the two intervals: placed on the value the save touched
    assert rule.status_code == 422
    assert [e["where"] for e in rule.json()["errors"]] == ["values.simkl.idle_sync_interval_minutes"]
    assert "lots-of-it-77" not in res.text and "no-such-theme-31" not in res.text
    assert ValidateResponse.model_validate(checked.json()).ok is False
    assert store.revision == 1


async def test_validate_warns_and_summarises_without_applying(store):
    async with client() as ac:
        res = await ac.post("/api/settings/validate", json={"values": {"mcp.enabled": True,
                                                                       "optimizer.max_active_services": 4}})
    body = ValidateResponse.model_validate(res.json())
    assert body.ok and body.summary == "Services kept at once 3 → 4; Allow AI assistants to read (MCP) off → on"
    assert [w.where for w in body.warnings] == ["values.mcp.enabled"]
    assert store.revision == 1 and app.state.container.config.mcp.enabled is False


async def test_the_conflict_rule_is_per_part(store):
    async with client() as ac:
        assert (await put(ac, 1, {"notify.hour": 7})).status_code == 200
        # another tab opened at revision 1 saves an untouched key: no conflict
        other = await put(ac, 1, {"optimizer.max_active_services": 2})
        assert other.status_code == 200 and other.json()["revision"] == 3
        # ...and one it touches that changed since: 409 with the parts
        clash = await put(ac, 1, {"notify.hour": 8, "notify.renewal_warning_days": 5})
        stale_slot = await put(ac, 3, secrets={"notify.url": "https://ntfy.example.com/a"})
        slot_clash = await put(ac, 3, secrets={"notify.url": "https://ntfy.example.com/b"})
    assert clash.status_code == 409
    assert clash.json() == {"detail": "Changed elsewhere since you opened the page", "revision": 3,
                            "conflicts": ["notify.hour"]}
    assert stale_slot.status_code == 200
    assert slot_clash.status_code == 409 and slot_clash.json()["conflicts"] == ["notify.url"]
    assert app.state.container.config.notify.renewal_warning_days == 3


async def test_versions_keep_fifty_and_a_restore_brings_one_back(store):
    async with client() as ac:
        for n in range(MAX_VERSIONS + 3):
            res = await put(ac, store.revision, {"notify.renewal_warning_days": 1 + n % 14})
            assert res.status_code == 200, res.text
            reset_rate_limits()
        versions = [VersionInfo.model_validate(v) for v in (await ac.get("/api/settings/versions")).json()]
        assert len(versions) == MAX_VERSIONS and versions[0].revision == store.revision
        assert versions[-1].revision == store.revision - MAX_VERSIONS + 1
        assert (await ac.get("/api/settings/versions/1")).status_code == 404
        old = versions[-1].revision
        draft = (await ac.get(f"/api/settings/versions/{old}")).json()
        restored = await put(ac, store.revision, draft["values"], draft["lists"], source="restore")
        latest = (await ac.get("/api/settings/versions")).json()[0]
    assert restored.status_code == 200
    assert latest["source"] == "restore" and latest["revision"] == restored.json()["revision"]
    assert store.document.values["notify.renewal_warning_days"] == draft["values"]["notify.renewal_warning_days"]


async def test_webhooks_are_added_with_their_address_and_never_show_it(store):
    async with client() as ac:
        missing = await put(ac, 1, lists={"webhooks": [{"name": "Home Assistant"}]})
        added = await put(ac, 1, lists={"webhooks": [{"name": "Home Assistant", "events": ["title.added"]}]},
                          secrets={"webhook.home-assistant.url": "https://192.0.2.10/hook-xyz"})
        state = (await ac.get("/api/settings")).json()
        restricted = await put(ac, 2, lists={"webhooks": [{"id": "home-assistant", "name": "Home Assistant",
                                                           "secret_env": "HOME"}]})
        filed = await put(ac, 2, lists={"webhooks": [{"id": "home-assistant", "name": "Home Assistant",
                                                      "secret_file": "/etc/shadow"}]})
    assert missing.status_code == 422 and missing.json()["errors"] == [
        {"where": "secrets.webhook.home-assistant.url", "message": "A new webhook needs its address"}]
    assert added.status_code == 200 and "Event webhooks: 1 added" in added.json()["summary"]
    assert "hook-xyz" not in added.text and "hook-xyz" not in str(state)
    assert state["lists"]["webhooks"]["items"] == [{"id": "home-assistant", "name": "Home Assistant",
                                                    "events": ["title.added"], "secret_env": None,
                                                    "secret_file": None}]
    assert state["lists"]["webhooks"]["states"] == {"home-assistant": {"state": "ok", "reason": None}}
    assert state["secrets"]["webhook.home-assistant.url"] == {"set": True, "source": "stored", "host": "192.0.2.10"}
    assert [h.url for h in app.state.container.config.webhooks] == ["https://192.0.2.10/hook-xyz"]
    assert restricted.status_code == 422 and filed.status_code == 422
    assert [e["where"] for e in restricted.json()["errors"]] == ["lists.webhooks[0].secret_env"]
    assert [e["where"] for e in filed.json()["errors"]] == ["lists.webhooks[0].secret_file"]


async def test_export_then_import_round_trips_without_secrets(store):
    async with client() as ac:
        await put(ac, 1, {"notify.hour": 7, "tvmaze.timeout_seconds": 15.0},
                  lists={"webhooks": [{"name": "Home"}]}, secrets={"webhook.home.url": "https://192.0.2.10/x-77"})
        export = await ac.get("/api/settings/export")
        assert export.status_code == 200 and export.headers["content-type"].startswith("text/yaml")
        assert export.headers["content-disposition"] == 'attachment; filename="penguwatch-settings.yaml"'
        text = export.text
        assert text.splitlines()[0] == "# PenguWatch settings (Settings > Export)" and "x-77" not in text
        data = yaml.safe_load(text)
        assert data["notify"]["hour"] == 7 and data["tvmaze"]["timeout_seconds"] == 15.0
        assert data["webhooks"][0]["id"] == "home" and "url" not in data["webhooks"][0]
        res = await ac.post("/api/settings/import", content=text, headers={"Content-Type": "text/yaml"})
    draft = ImportDraft.model_validate(res.json())
    assert draft.values == store.document.values and draft.lists == store.document.lists


async def test_import_of_a_whole_config_yaml_leaves_out_what_is_no_setting(store):
    text = ("server: {host: 0.0.0.0, port: 9000}\noptimizer: {max_active_services: 99, ready_to_binge_boost: 2}\n"
            "services: [{id: x, name: X}]\nwebhooks: [{name: a, url: 'https://192.0.2.1/secret-path-5'}]\n"
            "notify: {url: 'https://ntfy.example.com/t-41'}\n")
    async with client() as ac:
        res = await ac.post("/api/settings/import", content=text, headers={"Content-Type": "application/x-yaml"})
    draft = ImportDraft.model_validate(res.json())
    assert draft.values == {"optimizer.ready_to_binge_boost": 2.0}
    assert draft.kept == ["optimizer.max_active_services"]
    assert draft.lists["webhooks"][0]["name"] == "a" and "url" not in draft.lists["webhooks"][0]
    text_notes = " | ".join(draft.notes)
    assert "left in config.yaml: server.host, server.port" in text_notes
    assert "services: not imported; edited on the Services & Sync tab" in text_notes
    assert "notify.url: secrets are entered on the page" in text_notes and "1 webhook address in the file was not" \
        in text_notes
    assert "secret-path-5" not in res.text and "t-41" not in res.text and "99" not in res.text
    assert store.revision == 1  # nothing applied


@pytest.mark.parametrize("text,message", [
    ("a: &x [1]\nb: *x\n", "Anchors and aliases"),
    ("a: " + "[" * 40 + "]" * 40 + "\n", "That file is nested too deeply"),
    ("- a list\n", "A settings file is a YAML mapping"),
    ("a: [unclosed\nsecret-q9: 1\n", "That is not valid YAML at line"),
])
async def test_import_limits(store, text, message):
    async with client() as ac:
        res = await ac.post("/api/settings/import", content=text, headers={"Content-Type": "text/yaml"})
    assert res.status_code == 422 and res.json()["detail"].startswith(message)
    assert "secret-q9" not in res.text


async def test_import_size_and_type(store):
    async with client() as ac:
        big = await ac.post("/api/settings/import", content="a: " + "x" * (64 * 1024) + "\n",
                            headers={"Content-Type": "text/yaml"})
        json_body = await ac.post("/api/settings/import", json={"a": 1})
    assert big.status_code in (413, 422) and json_body.status_code == 415


async def test_every_settings_route_needs_the_token_when_one_is_set(store, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    bearer = {"Authorization": "Bearer s3cret-token"}
    routes = [("GET", "/api/settings", None), ("GET", "/api/settings/schema", None),
              ("GET", "/api/settings/values", None), ("GET", "/api/settings/versions", None),
              ("GET", "/api/settings/versions/1", None), ("GET", "/api/settings/export", None),
              ("POST", "/api/settings/validate", {"json": {}}),
              ("PUT", "/api/settings", {"json": {"base_revision": 1}}),
              ("POST", "/api/settings/import", {"content": "{}", "headers": {"Content-Type": "text/yaml"}})]
    async with client() as ac:
        for method, path, kw in routes:
            kw = dict(kw or {})
            assert (await ac.request(method, path, **kw)).status_code == 401, path
            kw["headers"] = {**kw.get("headers", {}), **bearer}
            assert (await ac.request(method, path, **kw)).status_code == 200, path
    paths = {p for p in app.openapi()["paths"] if p.startswith("/api/settings")}
    assert paths == {"/api/settings", "/api/settings/schema", "/api/settings/values", "/api/settings/validate",
                     "/api/settings/versions", "/api/settings/versions/{revision}", "/api/settings/export",
                     "/api/settings/import"}


async def test_the_answer_lists_every_key(store):
    async with client() as ac:
        state = SettingsState.model_validate((await ac.get("/api/settings")).json())
    assert set(state.values) == set(registry.BY_KEY) and state.revision == 1
    assert state.about.config_yaml.ignored_since is not None and state.about.import_report == []


async def test_a_bad_address_for_a_new_webhook_is_one_error_in_its_place(store):
    async with client() as ac:
        res = await put(ac, 1, lists={"webhooks": [{"name": "Home"}]},
                        secrets={"webhook.home.url": "https://user:pw-31@192.0.2.10/x"})
    assert res.status_code == 422 and res.json()["errors"] == [
        {"where": "secrets.webhook.home.url", "message": "Must not contain a user name or password."}]


# --- the webhook rules of a save (penguwatch.webhooks, wired in settings/validate.py) --------------------------------
# Every case goes through the real PUT /api/settings on a store whose config.yaml had three hooks: "ha" signs with an
# imported variable, "filed" with an imported container secret, "plain" is unsigned.

HOOKS_RAW = {"webhooks": [
    {"name": "HA", "id": "ha", "url": "https://192.0.2.10/hook-ha", "secret_env": "PENGUWATCH_WEBHOOK_HA"},
    {"name": "Filed", "id": "filed", "url": "https://192.0.2.11/hook-filed", "secret_file": "/run/secrets/filed"},
    {"name": "Plain", "id": "plain", "url": "https://192.0.2.12/hook-plain"}]}


@pytest.fixture
async def hooks(setup_test_app, tmp_path):
    return await attach_manager(app.state.container, tmp_path, HOOKS_RAW)


def _items(manager, **changes):
    """The stored items, with {id: {field: value}} changes."""
    items = [dict(i) for i in manager.document.lists["webhooks"]]
    for item in items:
        item.update(changes.get(item["id"], {}))
    return items


REFUSED_SAVES = {
    "secret_env names the access token": (
        lambda m: {"lists": {"webhooks": _items(m, plain={"secret_env": "PENGUWATCH_ACCESS_TOKEN"})}},
        "lists.webhooks[2].secret_env"),
    "secret_file is /etc/passwd": (
        lambda m: {"lists": {"webhooks": _items(m, plain={"secret_file": "/etc/passwd"})}},
        "lists.webhooks[2].secret_file"),
    "an imported secret_file changed": (
        lambda m: {"lists": {"webhooks": _items(m, filed={"secret_file": "/run/secrets/other"})}},
        "lists.webhooks[1].secret_file"),
    "a new hook names a variable": (
        lambda m: {"lists": {"webhooks": _items(m) + [{"name": "New", "secret_env": "PENGUWATCH_WEBHOOK_HA"}]},
                   "secrets": {"webhook.new.url": "https://192.0.2.20/hook-new"}},
        "lists.webhooks[3].secret_env"),
    "a credentialed URL": (
        lambda m: {"secrets": {"webhook.plain.url": "https://user:pw-77@192.0.2.12/hook-plain"}},
        "secrets.webhook.plain.url"),
    "loopback": (lambda m: {"secrets": {"webhook.plain.url": "http://127.0.0.1/"}}, "secrets.webhook.plain.url"),
    "cloud metadata": (lambda m: {"secrets": {"webhook.plain.url": "http://169.254.169.254/"}},
                       "secrets.webhook.plain.url"),
    "loopback as one number": (lambda m: {"secrets": {"webhook.plain.url": "http://2130706433/"}},
                               "secrets.webhook.plain.url"),
    "localhost as the digest address": (lambda m: {"secrets": {"notify.url": "http://localhost:8080/topic-77"}},
                                        "secrets.notify.url"),
    "a 15-character signing secret": (lambda m: {"secrets": {"webhook.plain.secret": "x" * 15}},
                                      "secrets.webhook.plain.secret"),
    "H1: a new address for a hook that keeps its imported variable": (
        lambda m: {"lists": {"webhooks": _items(m, ha={"name": "HA renamed"})},
                   "secrets": {"webhook.ha.url": "https://192.0.2.99/hook-elsewhere"}},
        "secrets.webhook.ha.url"),
    "H1 without the list in the save": (
        lambda m: {"secrets": {"webhook.ha.url": "https://192.0.2.99/hook-elsewhere"}}, "secrets.webhook.ha.url"),
    "H1 for a hook that keeps its imported file": (
        lambda m: {"secrets": {"webhook.filed.url": "https://192.0.2.99/hook-elsewhere"}},
        "secrets.webhook.filed.url"),
}


@pytest.mark.parametrize("case", list(REFUSED_SAVES))
async def test_a_save_that_breaks_a_webhook_rule_is_refused_in_its_place(hooks, case):
    body, where = REFUSED_SAVES[case]
    body = body(hooks)
    secrets_before, revision = hooks.secrets.snapshot(), hooks.revision
    async with client() as ac:
        res = await put(ac, revision, lists=body.get("lists"), secrets=body.get("secrets"))
    assert res.status_code == 422, (case, res.text)
    assert where in [e["where"] for e in res.json()["errors"]], (case, res.json())
    for echoed in ("ACCESS_TOKEN", "/etc/passwd", "/run/secrets/other", "pw-77", "169.254", "2130706433",
                   "topic-77", "x" * 15, "hook-elsewhere", "hook-new"):
        assert echoed not in res.text, (case, echoed)
    assert hooks.revision == revision and hooks.secrets.snapshot() == secrets_before  # nothing was saved


async def test_what_the_webhook_rules_allow(hooks):
    """The page keeps an imported variable or file, clears it, or swaps it for a stored secret with a new address; an
    unsigned hook moves freely; a private (RFC 1918) receiver is fine."""
    async with client() as ac:
        kept = await put(ac, 1, lists={"webhooks": _items(hooks, ha={"name": "HA renamed"},
                                                          filed={"events": ["title.added"]})})
        moved = await put(ac, 2, secrets={"webhook.plain.url": "http://192.168.1.20:8123/hook"})
        swapped = await put(ac, 3, lists={"webhooks": _items(hooks, ha={"secret_env": None})},
                            secrets={"webhook.ha.url": "https://192.0.2.99/hook", "webhook.ha.secret": "s" * 16})
        cleared = await put(ac, 4, lists={"webhooks": _items(hooks, filed={"secret_file": None})})
        state = (await ac.get("/api/settings")).json()
    assert [r.status_code for r in (kept, moved, swapped, cleared)] == [200, 200, 200, 200], \
        [r.text for r in (kept, moved, swapped, cleared)]
    items = {i["id"]: i for i in state["lists"]["webhooks"]["items"]}
    assert items["ha"]["secret_env"] is None and items["filed"]["secret_file"] is None
    assert state["secrets"]["webhook.ha.secret"] == {"set": True, "source": "stored", "host": None}


async def test_an_import_draft_saved_is_recorded_as_an_import(store):
    """The page sends PUT ?source=yaml_import for a draft read with Settings > Import (and ?source=restore for a version):
    the version list says which; anything else is refused."""
    async with client() as ac:
        draft = (await ac.post("/api/settings/import", content="notify:\n  hour: 6\n",
                               headers={"Content-Type": "text/yaml"})).json()
        saved = await put(ac, 1, draft["values"], draft["lists"], source="yaml_import")
        plain = await put(ac, 2, {"notify.hour": 7})
        odd = await put(ac, 3, {"notify.hour": 8}, source="anything")
        versions = (await ac.get("/api/settings/versions")).json()
    assert saved.status_code == 200 and plain.status_code == 200 and odd.status_code == 422
    assert [(v["revision"], v["source"]) for v in versions[:2]] == [(3, "save"), (2, "yaml_import")]
