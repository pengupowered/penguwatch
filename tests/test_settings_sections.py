"""The section models behind the Settings tab: every new default equals the constant or variable default it replaces,
the ranges refuse out-of-range values, the environment pins a setting through its field, the webhook ids, and the
frozen table of what the old build ran with (penguwatch.settings.legacy)."""

import pytest
from pydantic import ValidationError

from penguwatch.config import (
    AppConfig, EnvOverrides, HistoryConfig, NotifyConfig, OptimizerConfig, ServerConfig, WebhookConfig, WebhookItem,
    apply_env, load_config, read_config_file, webhook_id,
)
from penguwatch.drivers.simkl.options import SimklOptions
from penguwatch.settings import legacy, registry

_PINNING = ("PENGUWATCH_LOG_LEVEL", "PENGUWATCH_TZ", "PENGUWATCH_MCP_ENABLED", "PENGUWATCH_GRAFANA_URL",
            "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_NOTIFY_URL", "PENGUWATCH_CONFIG")


@pytest.fixture
def clean_env(monkeypatch):
    for name in _PINNING:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_the_new_defaults_equal_the_constants_they_replace():
    from penguwatch import history, notify
    from penguwatch.optimizer import engine
    assert NotifyConfig().renewal_warning_days == notify.RENEWAL_WARNING_DAYS
    assert HistoryConfig().bulk_min == history.BULK_MIN
    assert OptimizerConfig().value_window_days == engine.VALUE_WINDOW_DAYS
    assert OptimizerConfig().unused_hours == engine.UNUSED_HOURS
    assert (NotifyConfig().kind, NotifyConfig().hour) == ("ntfy", 9)  # the old PENGUWATCH_NOTIFY_KIND / _HOUR defaults


def test_every_default_but_one_equals_what_the_old_build_ran_with():
    assert set(legacy.OLD_DEFAULTS) == set(registry.BY_KEY)
    changed = {key for key, old in legacy.OLD_DEFAULTS.items() if old != registry.get(key).default()}
    assert changed == {"optimizer.max_active_services"}
    assert legacy.OLD_DEFAULTS["optimizer.max_active_services"] == 1
    assert set(legacy.OLD_EXAMPLE_VALUES) <= set(registry.BY_KEY)
    assert legacy.old_values(packaged_example=True)["optimizer.max_active_services"] == 3
    assert legacy.old_values(packaged_example=False)["optimizer.max_active_services"] == 1


def test_running_on_the_example_is_recognised():
    assert legacy.ran_on_example(None) and legacy.ran_on_example("/src/penguwatch/config.example.yaml")
    assert not legacy.ran_on_example("/app/config.yaml")


@pytest.mark.parametrize("model,field,bad", [
    (OptimizerConfig, "max_active_services", 0), (OptimizerConfig, "max_active_services", 11),
    (OptimizerConfig, "household_priority_boost", -0.5), (OptimizerConfig, "ready_to_binge_boost", 10.5),
    (OptimizerConfig, "value_window_days", 29), (OptimizerConfig, "value_window_days", 366),
    (OptimizerConfig, "unused_hours", -1), (OptimizerConfig, "unused_hours", 21),
    (NotifyConfig, "hour", 24), (NotifyConfig, "hour", -1), (NotifyConfig, "kind", "email"),
    (NotifyConfig, "renewal_warning_days", 0), (NotifyConfig, "renewal_warning_days", 15),
    (HistoryConfig, "bulk_min", 1), (HistoryConfig, "bulk_min", 21),
    (SimklOptions, "auto_sync_interval_minutes", 0), (SimklOptions, "auto_sync_interval_minutes", 1441),
    (SimklOptions, "idle_after_hours", 0), (SimklOptions, "idle_after_hours", 169),
    (SimklOptions, "idle_sync_interval_minutes", 1441), (SimklOptions, "app_name", ""),
    (SimklOptions, "app_name", "x" * 41), (SimklOptions, "app_name", "Pengu\nWatch"),
    (ServerConfig, "log_level", "verbose"), (ServerConfig, "timezone", "Mars/Olympus_Mons"),
    (ServerConfig, "timezone", "../etc/passwd"),
])
def test_a_value_outside_its_range_is_refused(model, field, bad):
    with pytest.raises(ValidationError):
        model(**{field: bad})


def test_the_edges_of_each_range_are_accepted():
    OptimizerConfig(max_active_services=1, household_priority_boost=0, ready_to_binge_boost=10, value_window_days=30,
                    unused_hours=0)
    OptimizerConfig(max_active_services=10, value_window_days=365, unused_hours=20)
    NotifyConfig(hour=0, renewal_warning_days=1)
    NotifyConfig(hour=23, renewal_warning_days=14)
    HistoryConfig(bulk_min=2)
    HistoryConfig(bulk_min=20)
    SimklOptions(auto_sync_interval_minutes=5, idle_sync_interval_minutes=15, idle_after_hours=168)


def test_the_quiet_interval_may_not_be_shorter_than_the_active_one():
    SimklOptions(auto_sync_interval_minutes=60, idle_sync_interval_minutes=60)
    with pytest.raises(ValidationError, match="idle_sync_interval_minutes"):
        SimklOptions(auto_sync_interval_minutes=60, idle_sync_interval_minutes=30)


def test_log_levels_and_zones_are_read_as_people_write_them():
    assert ServerConfig(log_level="INFO").log_level == "info"
    assert ServerConfig(log_level=" Warn ").log_level == "warning"
    assert ServerConfig(timezone=" America/New_York ").timezone == "America/New_York"
    assert ServerConfig(timezone="UTC").timezone == "UTC"
    assert ServerConfig(timezone="").timezone == ""
    assert NotifyConfig(kind="Webhook").kind == "webhook"


def test_a_grafana_url_setting_is_a_plain_base_url():
    assert AppConfig(display={"grafana_url": "https://grafana.example:3000/"}).display.grafana_url == (
        "https://grafana.example:3000")
    with pytest.raises(ValidationError, match="display.grafana_url"):
        AppConfig(display={"grafana_url": "https://user:pw@grafana.example"})


# --- the environment pins a setting through its field ----------------------------------------------------------------

def test_each_variable_pins_its_setting(clean_env, tmp_path):
    clean_env.setenv("PENGUWATCH_LOG_LEVEL", "DEBUG")
    clean_env.setenv("PENGUWATCH_TZ", "Europe/Berlin")
    clean_env.setenv("PENGUWATCH_MCP_ENABLED", "true")
    clean_env.setenv("PENGUWATCH_GRAFANA_URL", "https://grafana.example:3000/")
    clean_env.setenv("PENGUWATCH_NOTIFY_KIND", "Gotify")  # any value other than ntfy means a JSON webhook, as before
    clean_env.setenv("PENGUWATCH_NOTIFY_HOUR", "18")
    config = load_config(str(tmp_path / "none.yaml"))
    assert (config.server.log_level, config.server.timezone, config.mcp.enabled, config.display.grafana_url,
            config.notify.kind, config.notify.hour) == ("debug", "Europe/Berlin", True, "https://grafana.example:3000",
                                                         "webhook", 18)


def test_an_empty_variable_pins_nothing(clean_env, tmp_path):
    for name in _PINNING[:6]:
        clean_env.setenv(name, "  ")
    path = tmp_path / "config.yaml"
    path.write_text("server: {log_level: error, timezone: Asia/Tokyo}\nnotify: {kind: webhook, hour: 7}\n"
                    "mcp: {enabled: true}\n", encoding="utf-8")
    config = load_config(str(path))
    assert (config.server.log_level, config.server.timezone, config.notify.kind, config.notify.hour,
            config.mcp.enabled) == ("error", "Asia/Tokyo", "webhook", 7, True)


@pytest.mark.parametrize("name,value,key", [("PENGUWATCH_NOTIFY_HOUR", "25", "notify.hour"),
                                            ("PENGUWATCH_NOTIFY_HOUR", "nine", "notify.hour"),
                                            ("PENGUWATCH_TZ", "Mars/Olympus_Mons", "server.timezone"),
                                            ("PENGUWATCH_LOG_LEVEL", "loud", "server.log_level"),
                                            ("PENGUWATCH_MCP_ENABLED", "maybe", "mcp.enabled")])
def test_a_variable_its_field_refuses_is_ignored_with_a_warning_naming_it_not_its_value(clean_env, tmp_path, caplog,
                                                                                       name, value, key):
    """An invalid pinning variable does not stop the start (0.15 tolerated these). It is ignored,
    so the setting's own value applies and stays editable, and a warning names the variable, never its value."""
    import logging

    from penguwatch import config as config_module
    from penguwatch.config import env_pins
    config_module._warned.clear()
    clean_env.setenv(name, value)
    path = tmp_path / "config.yaml"
    path.write_text("server: {timezone: Asia/Tokyo, log_level: error}\nnotify: {hour: 7}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="penguwatch.config"):
        config = load_config(str(path))
    assert (config.server.timezone, config.server.log_level, config.notify.hour, config.mcp.enabled) == (
        "Asia/Tokyo", "error", 7, False)
    pinned, ignored = env_pins(EnvOverrides())
    assert key not in pinned and ignored[key].startswith(f"{name} is set but not valid")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(name in w for w in warnings) and not any(value in w for w in warnings)
    assert value not in ignored[key]


def test_apply_env_pins_exactly_the_variables_of_the_settings_env_table():
    from penguwatch.config import _SETTING_OVERRIDES
    from penguwatch.settings.env import SETTING_ENV
    assert {f"{section}.{field}": variable for _, section, field, variable in _SETTING_OVERRIDES} == SETTING_ENV


def test_apply_env_works_on_any_config(clean_env):
    clean_env.setenv("PENGUWATCH_NOTIFY_HOUR", "6")
    config = AppConfig()
    assert apply_env(config, EnvOverrides()) is config and config.notify.hour == 6


def test_read_config_file_returns_the_raw_mapping_and_the_path(clean_env, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("optimizer: {max_active_services: 99}\n", encoding="utf-8")
    assert read_config_file(str(path)) == (str(path), {"optimizer": {"max_active_services": 99}})
    assert read_config_file(str(tmp_path / "missing.yaml")) == (str(tmp_path / "missing.yaml"), {})
    path.write_text("- a list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_config_file(str(path))


# --- webhooks: the stored item, its id -------------------------------------------------------------------------------

def test_webhook_ids_come_from_the_name_and_are_unique():
    assert webhook_id("Home Assistant") == "home-assistant"
    assert webhook_id("  n8n.local ") == "n8n-local"
    assert webhook_id("!!!") == "webhook" and webhook_id(None) == "webhook"
    assert webhook_id("Home Assistant", {"home-assistant"}) == "home-assistant-2"
    assert webhook_id("Home Assistant", {"home-assistant", "home-assistant-2"}) == "home-assistant-3"
    assert len(webhook_id("x" * 60, {"x" * 40})) <= 40


def test_config_webhooks_get_ids_and_the_item_has_no_url():
    config = AppConfig(webhooks=[{"name": "Home Assistant", "url": "https://192.0.2.10/hook"},
                                 {"url": "https://hooks.example.net/a"},
                                 {"name": "Fixed", "id": "fixed-id", "url": "https://192.0.2.11/"}])
    assert [h.id for h in config.webhooks] == ["home-assistant", "hooks-example-net", "fixed-id"]
    assert "url" not in WebhookItem.model_fields and issubclass(WebhookConfig, WebhookItem)
    with pytest.raises(ValidationError, match="id may appear once"):
        AppConfig(webhooks=[{"name": "a", "id": "same", "url": "https://192.0.2.1/"},
                            {"name": "b", "id": "same", "url": "https://192.0.2.2/"}])


def test_a_webhook_error_never_echoes_its_input():
    secret_url = "ftp://user:hunter2-very-secret@192.0.2.10/hook"
    with pytest.raises(ValidationError) as refused:
        WebhookConfig(url=secret_url)
    assert "hunter2" not in str(refused.value)
    with pytest.raises(ValidationError) as refused:
        WebhookItem(id="Not An Id hunter2")
    assert "hunter2" not in str(refused.value)
