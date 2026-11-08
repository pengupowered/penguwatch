"""Tests for PenguWatch configuration."""

import os
from penguwatch.config import load_config, AppConfig


def test_load_config_defaults(tmp_path):
    config = load_config(str(tmp_path / "nonexistent.yaml"))
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 8080
    assert config.database.db_path == "data/penguwatch.db"
    assert config.optimizer.max_active_services == 3  # 1 before 0.16; the settings import keeps 1 where a file relied on it


def test_load_config_from_yaml(tmp_path):
    cfg_file = tmp_path / "test_config.yaml"
    cfg_file.write_text(
        """
server:
  host: "127.0.0.1"
  port: 9000
optimizer:
  max_active_services: 2
services:
  - id: "test_svc"
    name: "Test Stream"
    monthly_cost: 11.99
    is_active: true
"""
    )
    config = load_config(str(cfg_file))
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9000
    assert config.optimizer.max_active_services == 2
    assert len(config.services) == 1
    assert config.services[0].id == "test_svc"
    assert config.services[0].monthly_cost == 11.99


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("PENGUWATCH_PORT", "9999")
    monkeypatch.setenv("SIMKL_CLIENT_ID", "mock_client_id_123")
    config = load_config()
    assert config.server.port == 9999
    assert config.driver_options("simkl").client_id == "mock_client_id_123"


# --- env override behaviour pins (added with typed EnvOverrides) ---

import pytest

_ENV_NAMES = [
    "PENGUWATCH_HOST",
    "PENGUWATCH_PORT",
    "PENGUWATCH_LOG_LEVEL",
    "PENGUWATCH_DB_PATH",
    "SIMKL_CLIENT_ID",
    "SIMKL_ACCESS_TOKEN",
]

_YAML = """
server:
  host: "yaml-host"
  port: 9000
  log_level: "warning"
database:
  db_path: "yaml/path.db"
simkl:
  client_id: "yaml-id"
  access_token: "yaml-token"
tmdb:
  api_key: "yaml-key"
  region: "DE"
"""


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def yaml_path(tmp_path):
    p = tmp_path / "env_config.yaml"
    p.write_text(_YAML, encoding="utf-8")
    return str(p)


def _get(config, section, field):
    options = config.driver_options(section)  # a driver's section (simkl), else a section of the config
    return getattr(options if options is not None else getattr(config, section), field)


_ENV_FIELDS = [
    ("PENGUWATCH_HOST", "server", "host", "env-host", "yaml-host"),
    ("PENGUWATCH_LOG_LEVEL", "server", "log_level", "debug", "warning"),
    ("PENGUWATCH_DB_PATH", "database", "db_path", "env/path.db", "yaml/path.db"),
    ("SIMKL_CLIENT_ID", "simkl", "client_id", "env-id", "yaml-id"),
    ("SIMKL_ACCESS_TOKEN", "simkl", "access_token", "env-token", "yaml-token"),
]


@pytest.mark.parametrize("env,section,field,value,yaml_value", _ENV_FIELDS)
def test_env_var_overrides_yaml(clean_env, yaml_path, env, section, field, value, yaml_value):
    assert _get(load_config(yaml_path), section, field) == yaml_value
    clean_env.setenv(env, value)
    assert _get(load_config(yaml_path), section, field) == value


def test_env_port_overrides_yaml_and_is_int(clean_env, yaml_path):
    assert load_config(yaml_path).server.port == 9000
    clean_env.setenv("PENGUWATCH_PORT", "7777")
    port = load_config(yaml_path).server.port
    assert port == 7777
    assert isinstance(port, int)


@pytest.mark.parametrize("env,section,field,value,yaml_value", _ENV_FIELDS)
def test_empty_env_var_does_not_override(clean_env, yaml_path, env, section, field, value, yaml_value):
    clean_env.setenv(env, "")
    assert _get(load_config(yaml_path), section, field) == yaml_value


def test_empty_port_does_not_override(clean_env, yaml_path):
    clean_env.setenv("PENGUWATCH_PORT", "")
    assert load_config(yaml_path).server.port == 9000


def test_lowercase_env_var_is_ignored(clean_env, yaml_path):
    clean_env.setenv("penguwatch_host", "lower-host")
    config = load_config(yaml_path)
    assert config.server.host == "yaml-host"


def test_invalid_port_is_ignored(clean_env, yaml_path, tmp_path):
    clean_env.setenv("PENGUWATCH_PORT", "abc")
    assert load_config(yaml_path).server.port == 9000
    assert load_config(str(tmp_path / "nonexistent.yaml")).server.port == 8080


def test_removed_tmdb_settings_are_ignored(clean_env, yaml_path):
    """Old config.yaml files still carry a tmdb: block and old .env files a TMDB_API_KEY; neither may break loading."""
    clean_env.setenv("TMDB_API_KEY", "env-key")
    config = load_config(yaml_path)
    assert config.server.host == "yaml-host"
    assert not hasattr(config, "tmdb")


def test_main_listens_where_config_yaml_says(tmp_path, monkeypatch):
    """server.host / server.port in config.yaml decide where uvicorn listens; the env variables still win."""
    from penguwatch import main as app_main

    cfg = tmp_path / "config.yaml"
    cfg.write_text("server:\n  host: 127.0.0.1\n  port: 9123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for var in ("PENGUWATCH_HOST", "PENGUWATCH_PORT"):
        monkeypatch.delenv(var, raising=False)
    calls = []
    monkeypatch.setattr(app_main.uvicorn, "run", lambda app, **kw: calls.append(kw))
    app_main.main()
    assert calls[-1]["host"] == "127.0.0.1" and calls[-1]["port"] == 9123
    monkeypatch.setenv("PENGUWATCH_PORT", "9200")
    app_main.main()
    assert calls[-1]["port"] == 9200


# --- the variables outside config.yaml (EnvOverrides is the one place that reads them) -------------------------------

from penguwatch.config import env_pins  # noqa: E402

_NOTIFY_ENV = ("PENGUWATCH_NOTIFY_URL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR", "PENGUWATCH_GRAFANA_URL",
               "PENGUWATCH_LOG_FORMAT")


def test_the_notification_grafana_and_log_format_variables_have_their_documented_defaults(monkeypatch):
    from penguwatch.config import EnvOverrides
    for name in _NOTIFY_ENV:
        monkeypatch.delenv(name, raising=False)
    env = EnvOverrides()
    # notify_kind / notify_hour: None = not set, so the settings notify.kind / notify.hour (ntfy, 9) apply
    assert (env.notify_url, env.notify_kind, env.notify_hour, env.grafana_url, env.log_format) == ("", None, None, "", None)


def test_the_notification_grafana_and_log_format_variables_are_read(monkeypatch):
    from penguwatch.config import EnvOverrides
    monkeypatch.setenv("PENGUWATCH_NOTIFY_URL", "https://ntfy.example/topic")
    monkeypatch.setenv("PENGUWATCH_NOTIFY_KIND", "webhook")
    monkeypatch.setenv("PENGUWATCH_NOTIFY_HOUR", "18")
    monkeypatch.setenv("PENGUWATCH_GRAFANA_URL", " https://grafana.example:3000/ ")
    monkeypatch.setenv("PENGUWATCH_LOG_FORMAT", "json")
    env = EnvOverrides()
    # the pinning variables are kept as written; their settings' fields read them (config.apply_env, env_pins)
    assert (env.notify_url, env.notify_kind, env.notify_hour, env.grafana_url, env.log_format) == (
        "https://ntfy.example/topic", "webhook", "18", "https://grafana.example:3000", "json")
    assert env_pins(env)[0]["notify.hour"] == 18


def test_a_blank_notify_hour_is_the_default_and_a_garbled_one_is_ignored(monkeypatch):
    """An invalid pinning variable does not stop the start; it is ignored (the setting applies)."""
    from penguwatch.config import EnvOverrides
    monkeypatch.setenv("PENGUWATCH_NOTIFY_HOUR", "")
    assert EnvOverrides().notify_hour is None  # unset: the setting notify.hour (default 9) applies
    monkeypatch.setenv("PENGUWATCH_NOTIFY_HOUR", "nine")
    env = EnvOverrides()  # no error
    pinned, ignored = env_pins(env)
    assert "notify.hour" not in pinned and "PENGUWATCH_NOTIFY_HOUR" in ignored["notify.hour"]
    assert "nine" not in ignored["notify.hour"]


def test_a_grafana_url_that_is_not_a_plain_base_url_is_ignored(monkeypatch):
    from penguwatch.config import AppConfig, EnvOverrides, apply_env
    monkeypatch.setenv("PENGUWATCH_GRAFANA_URL", "https://user:pw@grafana.example")
    env = EnvOverrides()
    assert apply_env(AppConfig(), env).display.grafana_url == ""
    assert "PENGUWATCH_GRAFANA_URL is set but not valid" in env_pins(env)[1]["display.grafana_url"]
    assert "user:pw" not in env_pins(env)[1]["display.grafana_url"]


def test_notifier_and_logging_take_their_settings_from_env_overrides(monkeypatch):
    import logging
    from penguwatch.config import EnvOverrides
    from penguwatch.notify import Notifier
    from penguwatch.observability import JsonFormatter, configure_logging
    monkeypatch.setenv("PENGUWATCH_NOTIFY_URL", "https://hook.example/h")
    monkeypatch.setenv("PENGUWATCH_NOTIFY_KIND", "Webhook")
    monkeypatch.setenv("PENGUWATCH_LOG_FORMAT", "json")
    monkeypatch.setenv("PENGUWATCH_LOG_LEVEL", "debug")
    notifier = Notifier.from_env(EnvOverrides())
    assert notifier.enabled and notifier.kind == "webhook"
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        configure_logging(EnvOverrides())
        assert isinstance(root.handlers[0].formatter, JsonFormatter) and root.level == logging.DEBUG
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
