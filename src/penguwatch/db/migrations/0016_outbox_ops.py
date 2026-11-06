"""0016: the outbox holds neutral operations, each for one driver.

Before, a row held the exact SIMKL request body (payload_json) and the title's SIMKL id (simkl_id). Now:

- `driver`: the library driver the row is for; every existing row is SIMKL's.
- `remote_id` (was simkl_id): the title's id in that driver's library, which orders a title's rows and holds them
  behind a failed one.
- `op`: the neutral kind (watched, unwatched, status, rate, unrate, remove) instead of SIMKL's names.
- `payload_json`: the neutral operation (drivers.base.SyncOp: media type, the title's ids, episodes, watched_at,
  status, rating). A row written before keeps its original body under "native", which the SIMKL driver sends exactly
  as it is, so pending changes deliver the same bytes as before the driver layer.
"""

import json

import aiosqlite

KINDS = {"history_add": "watched", "history_remove": "unwatched", "list_move": "status", "rating_set": "rate",
         "rating_remove": "unrate", "remove_item": "remove"}


def _normal(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and not (len(text) > 1 and text.startswith("0")):
        return int(text) or None
    return text


def neutral(op: str, payload, media_type):
    """The neutral operation for a row as the pre-driver code wrote it (keeping the body itself as "native")."""
    payload = payload if isinstance(payload, dict) else {}
    items = [(bucket, it) for bucket in ("shows", "anime", "movies") for it in (payload.get(bucket) or [])
             if isinstance(it, dict)]
    bucket, item = items[0] if items else ("shows", {})
    ids = {}
    for key, value in (item.get("ids") or {}).items():
        v = _normal(value)
        if isinstance(key, str) and v is not None:
            ids[key] = v
    data = {"kind": KINDS.get(op, op), "media_type": media_type or ("movie" if bucket == "movies" else "show"),
            "ids": ids}
    episodes, watched_at = [], item.get("watched_at")
    for season in item.get("seasons") or []:
        for ep in (season or {}).get("episodes") or []:
            if isinstance(ep, dict) and season.get("number") is not None and ep.get("number") is not None:
                episodes.append([season["number"], ep["number"]])
                watched_at = watched_at or ep.get("watched_at")
    if episodes:
        data["episodes"] = episodes
    if watched_at:
        data["watched_at"] = watched_at
    status = payload.get("to") or item.get("to")
    if status:
        data["status"] = status
    if item.get("rating") is not None:
        data["rating"] = item["rating"]
    data["native"] = payload
    return data


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(sync_outbox)")
    cols = {row[1] for row in await cursor.fetchall()}
    await conn.execute("DROP INDEX IF EXISTS idx_outbox_simkl")
    if "simkl_id" in cols and "remote_id" not in cols:
        await conn.execute("ALTER TABLE sync_outbox RENAME COLUMN simkl_id TO remote_id")
    if "driver" not in cols:
        await conn.execute("ALTER TABLE sync_outbox ADD COLUMN driver TEXT")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_remote ON sync_outbox(driver, remote_id)")

    cursor = await conn.execute(
        "SELECT o.id, o.op, o.payload_json, s.media_type FROM sync_outbox o LEFT JOIN shows s ON s.id = o.show_id "
        "WHERE o.driver IS NULL")
    for row_id, op, raw, media_type in await cursor.fetchall():
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        await conn.execute(
            "UPDATE sync_outbox SET driver = 'simkl', op = ?, payload_json = ? WHERE id = ?",
            (KINDS.get(op, op), json.dumps(neutral(op, payload, media_type)), row_id))
