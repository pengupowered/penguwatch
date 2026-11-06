"""Helpers shared by the repository mixins."""

from penguwatch import clock


def _utcnow() -> str:
    return clock.utc_now().isoformat()
