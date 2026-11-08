"""The environment variables that override a setting or a secret slot, and the one rule for reading them.

A variable wins over the stored setting when it is set *and not empty*: the compose files pass `${PENGUWATCH_TZ:-}`,
so a variable nobody set arrives as "". Every reader of these variables goes through `env_value`, so that rule lives
in one place. The Settings tab shows a pinned value as "From environment · NAME" and refuses to save it.

This module imports nothing of the app (penguwatch.config imports it).
"""

import os
from typing import Dict, Mapping, Optional

# Registry key -> the variable that overrides it.
SETTING_ENV: Dict[str, str] = {
    "server.log_level": "PENGUWATCH_LOG_LEVEL",
    "server.timezone": "PENGUWATCH_TZ",
    "mcp.enabled": "PENGUWATCH_MCP_ENABLED",
    "display.grafana_url": "PENGUWATCH_GRAFANA_URL",
    "notify.kind": "PENGUWATCH_NOTIFY_KIND",
    "notify.hour": "PENGUWATCH_NOTIFY_HOUR",
}

# Secret slot -> the variable that overrides it (a save that writes the slot is refused while it is set). A webhook's
# signing secret is overridden by the variable its item names in `secret_env` (or the file in `secret_file`), which is
# per item and so not in this table.
SECRET_ENV: Dict[str, str] = {
    "notify.url": "PENGUWATCH_NOTIFY_URL",
}

OVERRIDE_VARIABLES = frozenset(SETTING_ENV.values()) | frozenset(SECRET_ENV.values())


def env_value(name: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The variable's value, stripped; None when it is unset, empty or only white space."""
    environ = os.environ if environ is None else environ
    value = (environ.get(name) or "").strip()
    return value or None


def setting_override(key: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The raw text of the variable that overrides registry key `key`, when one is set; else None."""
    name = SETTING_ENV.get(key)
    return env_value(name, environ) if name else None


def setting_overrides(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """{registry key: raw variable text} for every setting a variable pins right now."""
    found = {key: setting_override(key, environ) for key in SETTING_ENV}
    return {key: value for key, value in found.items() if value is not None}


def secret_override(slot: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The value of the variable that overrides secret slot `slot`, when one is set; else None. Never log it."""
    name = SECRET_ENV.get(slot)
    return env_value(name, environ) if name else None
