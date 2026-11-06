"""Single source of "now" and "today" for the household.

Stored timestamps stay UTC ISO strings (``utc_now()``); only day-level
decisions (renewal countdowns, air dates, weekday charts, notification hour)
use the configured zone via ``now()`` / ``today()``.
"""

import logging
import os
from datetime import date, datetime, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_zone: tzinfo = timezone.utc


def _resolve(zone_name: Optional[str]) -> tzinfo:
    name = (zone_name or "").strip() or os.environ.get("TZ", "").strip()
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # unknown name, malformed key or missing tzdata
        logger.warning("Unknown time zone %r; falling back to UTC.", name)
        return timezone.utc


def configure(zone_name: Optional[str]) -> None:
    """Set the household zone (IANA name). Empty uses the TZ env var, else UTC."""
    global _zone
    _zone = _resolve(zone_name)


def tz() -> tzinfo:
    return _zone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now() -> datetime:
    return utc_now().astimezone(_zone)


def today() -> date:
    return now().date()
