"""The checks of `server.timezone` and `server.log_level`, in plain pydantic terms (no import of the app, so
penguwatch.config can use them).

The zone is "" (the server's zone: TZ, else UTC, as penguwatch.clock reads it) or an IANA name zoneinfo can load
("UTC" included). The runtime and test images have /usr/share/zoneinfo; Windows has the tzdata package."""

import os
from typing import Optional

LOG_LEVELS = ("debug", "info", "warning", "error")
_LOG_ALIASES = {"warn": "warning"}


def known_zone(name: str) -> bool:
    """Whether zoneinfo can load `name` (an IANA zone such as "Europe/Berlin")."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not name or name.startswith(("/", ".")) or ".." in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def check_timezone(value: Optional[str]) -> str:
    """`value` stripped, when it is "" or a zone zoneinfo knows. Raises ValueError (without the value) otherwise."""
    if value is not None and not isinstance(value, str):
        raise ValueError("timezone must be text: an IANA time zone such as Europe/Berlin")
    value = (value or "").strip()
    if not value or value == "UTC" or known_zone(value):
        return value
    raise ValueError("timezone must be an IANA time zone such as America/New_York or Europe/Berlin, or empty for the "
                     "server's zone")


def server_timezone(environ=None) -> str:
    """What an empty `server.timezone` means right now: TZ when it names a zone, else "UTC" (penguwatch.clock)."""
    environ = os.environ if environ is None else environ
    tz = (environ.get("TZ") or "").strip()
    return tz if tz and known_zone(tz) else "UTC"


def normal_log_level(value):
    """A log level as config.yaml or the variable may write it ("INFO", "warn"), lower-cased; anything else is left
    for the Literal to refuse."""
    if isinstance(value, str):
        value = value.strip().lower()
        return _LOG_ALIASES.get(value, value)
    return value
