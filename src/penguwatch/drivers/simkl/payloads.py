"""SIMKL request bodies for neutral operations, and which of them may share one request.

Built so a change goes out exactly as before the driver layer: the same keys in the same order. An operation that
carries a `native` body (queued before the driver layer) is sent exactly as it is."""

from typing import Any, Dict, Hashable, List, Optional, Tuple

from penguwatch.drivers.base import (
    OP_RATE, OP_REMOVE, OP_STATUS, OP_UNRATE, OP_UNWATCHED, OP_WATCHED, SyncOp,
)

# Which SIMKL call each operation travels in (the path under /sync). Ops with the same call (and the same target
# status for a status change) can be merged into one request; ops of one title are never merged with each other.
ENDPOINTS = {
    OP_WATCHED: "/sync/history",
    OP_UNWATCHED: "/sync/history/remove",
    OP_REMOVE: "/sync/history/remove",
    OP_STATUS: "/sync/add-to-list",
    OP_RATE: "/sync/ratings",
    OP_UNRATE: "/sync/ratings/remove",
}
MAX_BATCH = 25  # titles per request


def simkl_ids(ids: Dict[str, Any]) -> Dict[str, Any]:
    """The ids SIMKL is sent for a title: its own, then IMDb's and TMDB's when known."""
    out: Dict[str, Any] = {"simkl": ids.get("simkl")}
    if ids.get("imdb"):
        out["imdb"] = ids["imdb"]
    if ids.get("tmdb"):
        out["tmdb"] = ids["tmdb"]
    return out


def bucket(op: SyncOp) -> str:
    return "movies" if op.media_type == "movie" else "shows"


def build(op: SyncOp) -> Dict[str, Any]:
    """The request body for one operation."""
    if op.native is not None:
        return op.native
    ids = simkl_ids(op.ids)
    if op.kind in (OP_WATCHED, OP_UNWATCHED):
        if op.media_type == "movie":
            item: Dict[str, Any] = {"ids": ids}
            if op.watched_at:
                item["watched_at"] = op.watched_at
            return {"movies": [item]}
        seasons: Dict[int, List[Dict[str, Any]]] = {}
        for s, n in sorted(set(op.episodes)):
            ep: Dict[str, Any] = {"number": n}
            if op.watched_at:
                ep["watched_at"] = op.watched_at
            seasons.setdefault(s, []).append(ep)
        return {"shows": [{"ids": ids, "seasons": [{"number": s, "episodes": e} for s, e in seasons.items()]}]}
    if op.kind == OP_STATUS:
        return {"to": op.status, bucket(op): [{"ids": ids, "to": op.status}]}
    if op.kind == OP_RATE:
        return {bucket(op): [{"ids": ids, "rating": op.rating}]}
    if op.kind in (OP_UNRATE, OP_REMOVE):
        return {bucket(op): [{"ids": ids}]}
    raise ValueError(f"Unknown outbox op {op.kind}")


def batch_key(op: SyncOp) -> Optional[Hashable]:
    """Ops with equal keys can share one request; unknown kinds never share (None)."""
    endpoint = ENDPOINTS.get(op.kind)
    if endpoint is None:
        return None
    to = op.native.get("to") if op.native is not None else (op.status if op.kind == OP_STATUS else None)
    return (endpoint, to)


def merge_payloads(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Concatenates the list-valued keys (shows/movies/anime); other keys (e.g. "to") must agree."""
    merged: Dict[str, Any] = {}
    for p in payloads:
        for k, v in p.items():
            if k not in merged:
                merged[k] = list(v) if isinstance(v, list) else v
            elif isinstance(v, list) and isinstance(merged[k], list):
                merged[k].extend(v)
            elif v != merged[k] or isinstance(v, list) or isinstance(merged[k], list):
                raise ValueError(f"cannot merge payloads: {k!r} differs")
    return merged


def request_for(ops: List[SyncOp]) -> Tuple[str, Dict[str, Any]]:
    """(path, body) of the one request that delivers `ops` (one op, or several sharing a batch_key)."""
    endpoint = ENDPOINTS.get(ops[0].kind)
    if endpoint is None:
        raise ValueError(f"Unknown outbox op {ops[0].kind}")
    if len(ops) == 1:
        return endpoint, build(ops[0])
    return endpoint, merge_payloads([build(op) for op in ops])
