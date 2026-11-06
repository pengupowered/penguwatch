"""Neutral sync operations (drivers.base.SyncOp): building them from a title, and describing them to a person."""

from typing import Iterable, List, Optional, Tuple

from penguwatch.db.episodes import format_episode_code
from penguwatch.drivers.base import (
    OP_RATE, OP_REMOVE, OP_STATUS, OP_UNRATE, OP_UNWATCHED, OP_WATCHED, SyncOp,
)
from penguwatch.models import Show

STATUS_LABELS = {"watching": "Watching", "plantowatch": "Plan to Watch", "hold": "On Hold", "completed": "Completed",
                 "dropped": "Dropped"}


def describe_episodes(keys) -> str:
    keys = sorted(set(keys))
    if not keys:
        return "no episodes"
    if len(keys) == 1:
        return format_episode_code(*keys[0])
    if len(keys) <= 3:
        return ", ".join(format_episode_code(s, n) for s, n in keys)
    return f"{format_episode_code(*keys[0])} – {format_episode_code(*keys[-1])} ({len(keys)} episodes)"


def describe(op: SyncOp, library_label: str = "the library") -> str:
    """What the operation does, in a few words ("Mark S01E02 watched")."""
    if op.kind == OP_STATUS:
        return f"Set status to {STATUS_LABELS.get(op.status or '', op.status)}"
    if op.kind in (OP_WATCHED, OP_UNWATCHED):
        verb = "watched" if op.kind == OP_WATCHED else "unwatched"
        if op.media_type == "movie":
            return f"Mark movie {verb}"
        return f"Mark {describe_episodes(op.episodes)} {verb}" if op.episodes else f"Mark whole title {verb}"
    if op.kind == OP_RATE:
        return f"Rate {op.rating}/10"
    if op.kind == OP_UNRATE:
        return "Remove rating"
    if op.kind == OP_REMOVE:
        return f"Remove from {library_label} (lists and watch history)"
    return op.kind


def _base(kind: str, show: Show) -> SyncOp:
    return SyncOp(kind=kind, media_type=show.media_type.value, ids=dict(show.ids))


def episodes_op(show: Show, keys: Iterable[Tuple[int, int]], watched: bool, watched_at: Optional[str]) -> SyncOp:
    op = _base(OP_WATCHED if watched else OP_UNWATCHED, show)
    op.episodes = sorted(set((int(s), int(n)) for s, n in keys))
    op.watched_at = watched_at if watched else None
    return op


def movie_op(show: Show, watched: bool, watched_at: Optional[str]) -> SyncOp:
    op = _base(OP_WATCHED if watched else OP_UNWATCHED, show)
    op.watched_at = watched_at if watched else None
    return op


def status_op(show: Show, status: str) -> SyncOp:
    op = _base(OP_STATUS, show)
    op.status = status
    return op


def rating_op(show: Show, rating: Optional[int]) -> SyncOp:
    op = _base(OP_RATE if rating is not None else OP_UNRATE, show)
    op.rating = rating
    return op


def remove_op(show: Show) -> SyncOp:
    return _base(OP_REMOVE, show)


__all__: List[str] = ["STATUS_LABELS", "describe", "describe_episodes", "episodes_op", "movie_op", "status_op",
                      "rating_op", "remove_op"]
