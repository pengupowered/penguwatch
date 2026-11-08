"""The settings registry's contract (penguwatch.settings.registry): every entry names a real field, every field of a
stored section is on the page or deliberately left off, the page order and the words are complete, and nothing in
the registry repeats a default or a range (those live on the pydantic fields only)."""

import dataclasses

import pytest

from penguwatch.config import AppConfig
from penguwatch.settings import env as settings_env
from penguwatch.settings import registry
from penguwatch.settings.registry import LISTS, NOT_ON_PAGE, SECRET_SLOTS, SECTION_IDS, SETTINGS, Setting

# The 27 settings by section, in page order
EXPECTED = {
    "household": ["household.members"],
    "sync": ["simkl.auto_sync_interval_minutes", "simkl.idle_after_hours", "simkl.idle_sync_interval_minutes"],
    "optimizer": ["optimizer.max_active_services", "optimizer.household_priority_boost",
                  "optimizer.ready_to_binge_boost"],
    "notifications": ["notify.kind", "notify.hour", "notify.renewal_warning_days"],
    "display": ["display.theme", "server.timezone"],
    "integrations": ["mcp.enabled", "display.grafana_url"],
    "sources": ["drivers.library", "drivers.catalog", "drivers.schedule", "drivers.discovery"],
    "advanced": ["optimizer.value_window_days", "optimizer.unused_hours", "history.bulk_min", "server.log_level",
                 "simkl.app_name", "tvmaze.schedule_max_titles", "tvmaze.rate_limit_calls",
                 "tvmaze.rate_window_seconds", "tvmaze.timeout_seconds"],
}
# The variables that pin a setting or a slot
OVERRIDES = {
    "server.log_level": "PENGUWATCH_LOG_LEVEL", "server.timezone": "PENGUWATCH_TZ",
    "mcp.enabled": "PENGUWATCH_MCP_ENABLED", "display.grafana_url": "PENGUWATCH_GRAFANA_URL",
    "notify.kind": "PENGUWATCH_NOTIFY_KIND", "notify.hour": "PENGUWATCH_NOTIFY_HOUR",
}
RESTART = {"drivers.library", "drivers.catalog", "drivers.schedule", "drivers.discovery", "tvmaze.rate_limit_calls",
           "tvmaze.rate_window_seconds", "tvmaze.timeout_seconds", "simkl.app_name"}


def test_the_sections_hold_the_designed_settings_in_page_order():
    assert [s.id for s in registry.SECTIONS] == list(EXPECTED)
    assert {section: [s.key for s in registry.in_section(section)] for section in SECTION_IDS} == EXPECTED
    assert len(SETTINGS) == 27


def test_every_registry_key_resolves_to_a_field():
    for setting in SETTINGS:
        model, name, info = setting.resolve()
        assert name in model.model_fields and info is model.model_fields[name], setting.key


def test_a_drivers_options_are_declared_by_the_driver_and_grouped_under_its_label():
    """The registry names no service: each driver lists the options the page shows (drivers.base.DriverSetting)."""
    from penguwatch.drivers import DRIVERS
    for setting in SETTINGS:
        if setting.driver is not None:
            driver = DRIVERS[setting.driver]
            assert setting.key.startswith(f"{driver.name}.") and setting.group == driver.label, setting.key
            row = next(r for r in driver.settings if f"{driver.name}.{r.field}" == setting.key)
            assert (setting.label, setting.live, setting.section) == (row.label, row.live, row.section)
    assert {s.key for s in SETTINGS if s.driver} == {k for keys in EXPECTED.values() for k in keys
                                                     if k.startswith(("simkl.", "tvmaze."))}


def test_a_driver_option_resolves_to_the_drivers_options_model():
    from penguwatch.drivers.simkl.options import SimklOptions
    from penguwatch.drivers.tvmaze.options import TvmazeOptions
    assert registry.get("simkl.app_name").resolve()[0] is SimklOptions
    assert registry.get("tvmaze.timeout_seconds").resolve()[0] is TvmazeOptions


@pytest.mark.parametrize("key", ["optimizer.no_such_weight", "optimizer.max_active_services.deeper", "nosection.x",
                                 "local.anything", "driver_sections.simkl", "optimizer", "simkl.no_such_option"])
def test_a_key_that_names_no_field_is_refused(key):
    with pytest.raises(KeyError):
        registry._resolve(key)


def test_every_field_of_a_stored_section_is_on_the_page_or_listed_as_left_off():
    """Fails when a section model (or a driver's options) gains a field and nobody decided whether the page shows
    it."""
    assert registry.unplaced_fields() == [], "add these to the registry, LISTS or NOT_ON_PAGE"
    leaves = registry.stored_leaf_paths()
    assert set(NOT_ON_PAGE) <= set(leaves)  # nothing listed points at a field that is gone
    assert {spec.path for spec in LISTS.values()} <= set(leaves)
    assert "driver_sections" not in leaves


def test_a_section_that_gains_a_field_without_an_entry_is_named():
    from pydantic import create_model

    from penguwatch.config import NotifyConfig, OptimizerConfig
    grown_optimizer = create_model("GrownOptimizer", __base__=OptimizerConfig, brand_new_weight=(float, 1.0))
    grown_notify = create_model("GrownNotify", __base__=NotifyConfig, quiet_hour=(int, 22))
    grown = create_model("GrownConfig", __base__=AppConfig, optimizer=(grown_optimizer, grown_optimizer()),
                         notify=(grown_notify, grown_notify()), brand_new_section=(int, 1))
    assert registry.unplaced_fields(grown) == ["optimizer.brand_new_weight", "notify.quiet_hour", "brand_new_section"]


def test_a_driver_option_without_an_entry_is_named(monkeypatch):
    from pydantic import create_model

    from penguwatch.drivers.tvmaze import TvmazeDriver
    from penguwatch.drivers.tvmaze.options import TvmazeOptions
    grown = create_model("GrownTvmaze", __base__=TvmazeOptions, retry_after_seconds=(int, 5))
    monkeypatch.setattr(TvmazeDriver, "options_model", grown)
    assert registry.unplaced_fields() == ["tvmaze.retry_after_seconds"]


def test_the_list_and_the_secret_slots():
    assert set(LISTS) == {"webhooks"} and LISTS["webhooks"].path == "webhooks"
    assert LISTS["webhooks"].max_items == AppConfig.model_fields["webhooks"].metadata[0].max_length
    assert [s.slot for s in SECRET_SLOTS] == ["notify.url", "webhook.{id}.url", "webhook.{id}.secret"]
    assert registry.slot_kind("webhook.home-hook.url").slot == "webhook.{id}.url"
    assert registry.slot_kind("webhook.home.secret").slot == "webhook.{id}.secret"
    assert registry.slot_kind("notify.url").env == "PENGUWATCH_NOTIFY_URL"
    for name in ("webhook..url", "webhook.Home.url", "webhook.home.token", "notify.token", "webhook.a.b.url"):
        assert registry.slot_kind(name) is None, name


def test_bootstrap_keys_and_credentials_are_not_on_the_page():
    for key in ("server.host", "server.port", "database.db_path", "simkl.client_id", "simkl.access_token", "services",
                "ratings.sources"):
        assert key in NOT_ON_PAGE and key not in registry.BY_KEY


def test_keys_are_unique_and_sections_known_and_ordered():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys))
    for setting in SETTINGS:
        assert setting.section in SECTION_IDS, setting.key
        assert setting.label.strip() and setting.effect.strip(), setting.key
        assert setting.advanced == (setting.section == "advanced")
    for section in SECTION_IDS:
        orders = [s.order for s in SETTINGS if s.section == section]
        assert orders == list(range(1, len(orders) + 1)), section


def test_every_env_name_is_one_of_the_override_variables():
    assert {s.key: s.env for s in SETTINGS if s.env} == OVERRIDES == settings_env.SETTING_ENV
    used = {s.env for s in SETTINGS if s.env} | {s.env for s in SECRET_SLOTS if s.env}
    assert used == set(settings_env.OVERRIDE_VARIABLES) == set(OVERRIDES.values()) | {"PENGUWATCH_NOTIFY_URL"}


def test_only_the_designed_settings_wait_for_a_restart():
    assert {s.key for s in SETTINGS if not s.live} == RESTART


def test_no_setting_repeats_a_default_a_type_or_a_range():
    fields = {f.name for f in dataclasses.fields(Setting)}
    assert fields.isdisjoint({"default", "type", "min", "max", "ge", "gt", "le", "lt", "choices", "nullable",
                              "step", "max_length", "min_length"})


def test_every_choice_has_a_label_and_every_label_a_choice():
    for setting in SETTINGS:
        choices = setting.constraints()["choices"]
        if setting.choice_labels is not None and choices is not None:
            assert set(setting.choice_labels) == set(choices), setting.key


def test_constraints_and_defaults_come_from_the_fields():
    found = registry.get("optimizer.max_active_services").constraints()
    assert registry.get("optimizer.max_active_services").default() == 3
    assert (found["type"], found["min"], found["max"], found["step"], found["nullable"]) == ("integer", 1, 10, 1, False)
    hour = registry.get("notify.hour")
    assert (hour.default(), hour.constraints()["type"], hour.constraints()["min"], hour.constraints()["max"]) == (
        9, "hour", 0, 23)
    assert registry.get("notify.kind").constraints()["choices"] == ["ntfy", "webhook"]
    assert registry.get("server.log_level").constraints()["choices"] == ["debug", "info", "warning", "error"]
    assert registry.get("server.timezone").constraints()["type"] == "timezone"
    assert registry.get("display.theme").constraints()["type"] == "theme"
    members = registry.get("household.members").constraints()
    assert (members["type"], members["min_length"], members["max_length"]) == ("names", 1, 2)
    assert registry.get("drivers.library").constraints()["type"] == "driver"
    assert registry.get("drivers.schedule").constraints()["type"] == "drivers"
    assert registry.get("drivers.catalog").default() == ["simkl"]
    window = registry.get("tvmaze.rate_window_seconds").constraints()
    assert (window["type"], window["min"], window["exclusive_min"], window["max"]) == ("number", 0, True, 60)
    sync = registry.get("simkl.auto_sync_interval_minutes").constraints()
    assert (sync["min"], sync["max"]) == (5, 1440)
