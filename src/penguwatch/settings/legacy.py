"""What the build before the settings store (main 80aebed, the 0.16 work without Settings) ran with, for the one-time
import: a registry key the config file does not set is stored when the old build's value for it
differs from the new default, so behaviour does not change at the upgrade.

Frozen tables, written from 80aebed's models and its packaged config.example.yaml before they changed; never edit them
to follow a model (tests/test_settings_sections.py pins them). Today the only difference is
optimizer.max_active_services: the model said 1, the packaged example (and every install running on it) said 3, the
new default is 3."""

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from penguwatch.drivers import DEFAULT_CHAIN, DEFAULT_LIBRARY, DRIVERS

# The model defaults of 80aebed for every registry key: what a key that config.yaml leaves out meant. Keys that were
# not settings then (the module constants and the PENGUWATCH_NOTIFY_* defaults) have the value the code used.
_CORE_OLD_DEFAULTS: Dict[str, Any] = {
    "household.members": ["Me", "Partner"],
    "optimizer.max_active_services": 1,
    "optimizer.household_priority_boost": 2.0,
    "optimizer.ready_to_binge_boost": 1.5,
    "notify.kind": "ntfy",                  # EnvOverrides.notify_kind default
    "notify.hour": 9,                       # EnvOverrides.notify_hour default
    "notify.renewal_warning_days": 3,       # notify.RENEWAL_WARNING_DAYS
    "display.theme": "default",
    # no theme had an effect then: "on" changes nothing for a browser that has none to run
    "display.effects": True,
    "server.timezone": "",
    "mcp.enabled": False,
    "display.grafana_url": "",              # EnvOverrides.grafana_url default
    # 80aebed: every role defaulted to the default library driver (penguwatch.drivers.DEFAULT_LIBRARY / DEFAULT_CHAIN)
    "drivers.library": DEFAULT_LIBRARY,
    "drivers.catalog": list(DEFAULT_CHAIN),
    "drivers.schedule": list(DEFAULT_CHAIN),
    "drivers.discovery": list(DEFAULT_CHAIN),
    "optimizer.value_window_days": 90,      # optimizer.engine.VALUE_WINDOW_DAYS
    "optimizer.unused_hours": 0.5,          # optimizer.engine.UNUSED_HOURS
    "history.bulk_min": 3,                  # history.BULK_MIN
    "server.log_level": "info",
}

# The model defaults of 80aebed for every registry key: what a key that config.yaml leaves out meant. Keys that were
# not settings then (the module constants and the PENGUWATCH_NOTIFY_* defaults) have the value the code used. A
# driver's options come from its own frozen `legacy_defaults` (the driver package names its service, this module
# does not).
OLD_DEFAULTS: Mapping[str, Any] = {
    **_CORE_OLD_DEFAULTS,
    **{f"{name}.{option}": value for name, driver in sorted(DRIVERS.items())
       for option, value in getattr(driver, "legacy_defaults", {}).items()},
}

# The registry keys 80aebed's packaged config.example.yaml set to something else than OLD_DEFAULTS, or set at all. A
# native install without a config file ran on that example (cli.cmd_serve, config.load_config), so for it these were
# the old values. (The example also set the library driver's four options, to their legacy_defaults.)
OLD_EXAMPLE_VALUES: Mapping[str, Any] = {
    "server.log_level": "info",
    "household.members": ["Me", "Partner"],
    "display.theme": "default",
    "mcp.enabled": False,
    "optimizer.max_active_services": 3,
    "optimizer.household_priority_boost": 2.0,
    "optimizer.ready_to_binge_boost": 1.5,
}


EXAMPLE_FILE = "config.example.yaml"


def ran_on_example(config_path: Optional[str]) -> bool:
    """Whether the old build ran on its example: config.read_config_file found no file (None: the copy inside the
    package) or found a config.example.yaml (a checkout without a config.yaml)."""
    return config_path is None or Path(config_path).name == EXAMPLE_FILE


def old_values(packaged_example: bool) -> Dict[str, Any]:
    """{registry key: what the old build ran with when the file did not set the key}. `packaged_example`:
    ran_on_example(the path read), so the old build ran on its example's values."""
    values = dict(OLD_DEFAULTS)
    if packaged_example:
        values.update(OLD_EXAMPLE_VALUES)
    return values
