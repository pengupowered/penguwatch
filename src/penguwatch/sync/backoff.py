"""Back-off bookkeeping for background catalogue refreshes that failed or came back empty.

Pure functions over a small dict {"episodes:<show_id>": "<iso utc>", ...} kept in app_state, so a title
that will never succeed is not retried every cycle (and cannot starve others under the per-cycle cap).
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

STATE_KEY = "sync.refresh_attempts"
BACKOFF_HOURS = 24
KEEP_HOURS = 24 * 14


def load(raw: Optional[str]) -> Dict[str, str]:
    """Parses the stored value; a missing or corrupt value (or non-string entries) yields what is usable."""
    if not raw:
        return {}
    try:
        data: Any = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def dump(attempts: Dict[str, str]) -> str:
    return json.dumps(attempts, sort_keys=True)


def _parse(ts: str) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def due(attempts: Dict[str, str], key: str, now: datetime, hours: float = BACKOFF_HOURS) -> bool:
    """True if `key` has no failed attempt within the last `hours` (unparsable entries don't block)."""
    ts = attempts.get(key)
    when = _parse(ts) if ts else None
    return when is None or now - when >= timedelta(hours=hours)


def record(attempts: Dict[str, str], key: str, now: datetime) -> Dict[str, str]:
    return {**attempts, key: now.astimezone(timezone.utc).isoformat()}


def clear(attempts: Dict[str, str], key: str) -> Dict[str, str]:
    return {k: v for k, v in attempts.items() if k != key}


def prune(attempts: Dict[str, str], now: datetime, keep_hours: float = KEEP_HOURS) -> Dict[str, str]:
    """Drops entries older than `keep_hours` (and unparsable ones) so the dict stays small."""
    out = {}
    for key, ts in attempts.items():
        when = _parse(ts)
        if when is not None and now - when < timedelta(hours=keep_hours):
            out[key] = ts
    return out
