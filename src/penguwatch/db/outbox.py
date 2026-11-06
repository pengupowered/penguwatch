"""Sync outbox queries for the repository: local changes waiting to reach the library driver.

A row is one neutral operation (drivers.base.SyncOp, in payload_json) for one driver (`driver`), about the title
`show_id`, whose id in that driver's library is `remote_id`. Rows are deleted once the driver accepted them, so
nothing is lost while the service is down or the app restarts."""

import json
from typing import Any, Dict, List, Optional, Tuple

from penguwatch.db._common import _utcnow
from penguwatch.drivers.base import SyncOp


def _row(r) -> Dict[str, Any]:
    d = dict(r)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    d["operation"] = SyncOp.from_json(d["payload"], row_id=d["id"])
    return d


# 1: held until a full pull has shown what the account has (SyncOp.if_missing: "add", "edit", or a plain true)
_FLAG = "(CASE WHEN json_extract(payload_json, '$.if_missing') IS NULL THEN 0 ELSE 1 END)"

_CONTRADICTED_BY = {  # the new op's kind -> the kinds of older unapproved rows it replaces
    "status": ("status", "remove"),  # a newer status (an add, a re-add) undoes an older unapproved removal
    "rate": ("rate", "unrate"), "unrate": ("rate", "unrate"),
}


async def _supersede_tx(db, driver: str, op: SyncOp, remote_id: Any) -> bool:
    """A newer change replaces the older UNAPPROVED rows of the same title and driver that it contradicts, so that
    approving the old ones later cannot undo it: status replaces status (and an older removal: the title was added
    again); rate and unrate replace each other; remove replaces every row; watched and unwatched take their episodes
    out of the older watched/unwatched rows (a row left with no episodes goes; a movie, which has none, replaces them
    like a status). Approved rows are never touched (they go first, in id order).

    Rows with a `native` body (queued before the driver layer) are replaced whole where a whole-row rule applies, but
    cannot be trimmed episode by episode: when a newer watched/unwatched change meets an older native one, the older
    row stays and True is returned, which makes the newer row unapproved too, so that nothing can go out of order and
    the person decides. Runs in the enqueue's transaction."""
    if remote_id is None:
        return False
    base = "SELECT id, op, payload_json FROM sync_outbox WHERE driver = ? AND remote_id = ? AND approved = 0"
    rows = await (await db.execute(base, (driver, remote_id))).fetchall()
    drop: List[int] = []
    hold = False
    if op.kind == "remove":
        drop = [r["id"] for r in rows]
    elif op.kind in _CONTRADICTED_BY:
        drop = [r["id"] for r in rows if r["op"] in _CONTRADICTED_BY[op.kind]]
    elif op.kind in ("watched", "unwatched"):
        from penguwatch.sync import ops as sync_ops  # here: the sync package imports the database package
        taking = set(map(tuple, op.episodes))
        for r in rows:
            if r["op"] not in ("watched", "unwatched"):
                continue
            payload = json.loads(r["payload_json"])
            if "native" in payload:
                hold = True
                continue
            if not op.episodes:  # a movie
                if not payload.get("episodes"):
                    drop.append(r["id"])
                continue
            left = [e for e in payload.get("episodes") or [] if tuple(e) not in taking]
            if len(left) == len(payload.get("episodes") or []):
                continue
            if not left:
                drop.append(r["id"])
            else:
                payload["episodes"] = left
                await db.execute("UPDATE sync_outbox SET payload_json = ?, summary = ? WHERE id = ?",
                                 (json.dumps(payload), sync_ops.describe(SyncOp.from_json(payload)), r["id"]))
    if drop:
        await db.execute(f"DELETE FROM sync_outbox WHERE id IN ({','.join('?' * len(drop))})", drop)
    return hold


class OutboxMixin:
    # --- Sync outbox (pending pushes to the library driver) ---

    async def enqueue_outbox(
        self,
        driver: str,
        op: SyncOp,
        show_id: Optional[int],
        remote_id: Any,
        summary: Optional[str] = None,
        approved: bool = True,
    ) -> int:
        async with self.write() as db:
            if await _supersede_tx(db, driver, op, remote_id):
                approved = False  # it meets an older native watch row it cannot trim: the person decides
            cursor = await db.execute(
                "INSERT INTO sync_outbox (driver, show_id, remote_id, op, payload_json, next_attempt_at, summary, approved) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (driver, show_id, remote_id, op.kind, json.dumps(op.to_json()), _utcnow(), summary, 1 if approved else 0),
            )
        return cursor.lastrowid

    async def get_due_outbox(self, driver: str, limit: int = 50, after_id: int = 0) -> List[Dict[str, Any]]:
        """The next `limit` approved rows that are due, oldest first, with an id above `after_id` (to read on from where
        the previous call stopped)."""
        db = await self.read_connection()
        cursor = await db.execute(
            f"SELECT * FROM sync_outbox WHERE driver = ? AND approved = 1 AND {_FLAG} = 0 AND id > ? "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id ASC LIMIT ?",
            (driver, after_id, _utcnow(), limit),
        )
        return [_row(r) for r in await cursor.fetchall()]

    async def delete_outbox(self, outbox_id: int) -> None:
        async with self.write() as db:
            await db.execute("DELETE FROM sync_outbox WHERE id = ?", (outbox_id,))

    async def outbox_hold_points(self, driver: str) -> Dict[Any, int]:
        """Per title (remote_id): the oldest approved row that is backing off after a failed delivery. Newer rows of
        that title must wait behind it so a title's changes reach the service in order. Rows still waiting for
        approval (review mode) are not listed: they are the user's to decide on and never block approved changes.
        Rows held until a full pull (`if_missing`) do hold the newer rows of their title, approved or not."""
        db = await self.read_connection()
        cursor = await db.execute(
            f"SELECT remote_id, MIN(id) AS hold FROM sync_outbox WHERE driver = ? AND "
            f"((approved = 1 AND next_attempt_at IS NOT NULL AND next_attempt_at > ?) OR {_FLAG} = 1) GROUP BY remote_id",
            (driver, _utcnow()),
        )
        return {r["remote_id"]: r["hold"] for r in await cursor.fetchall()}

    async def delete_outbox_many(self, outbox_ids: List[int]) -> None:
        if not outbox_ids:
            return
        q = ",".join("?" * len(outbox_ids))
        async with self.write() as db:
            await db.execute(f"DELETE FROM sync_outbox WHERE id IN ({q})", list(outbox_ids))

    async def fail_outbox(self, outbox_id: int, error: str, next_attempt_at: str) -> None:
        async with self.write() as db:
            await db.execute(
                "UPDATE sync_outbox SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? WHERE id = ?",
                (error[:500], next_attempt_at, outbox_id),
            )

    async def get_outbox(self) -> List[Dict[str, Any]]:
        db = await self.read_connection()
        cursor = await db.execute(
            """
            SELECT o.id, o.driver, o.show_id, o.remote_id, o.op, o.attempts, o.last_error, o.next_attempt_at,
                   o.created_at, o.approved, o.summary, o.payload_json, s.title AS title, s.en_title AS en_title
            FROM sync_outbox o LEFT JOIN shows s ON o.show_id = s.id ORDER BY o.id ASC
            """
        )
        rows = []
        for r in await cursor.fetchall():
            d = _row(r)
            d["approved"] = bool(d["approved"])
            rows.append(d)
        return rows

    async def approve_outbox(self, ids: Optional[List[int]] = None) -> int:
        async with self.write() as db:
            if ids is None:
                cursor = await db.execute("UPDATE sync_outbox SET approved = 1, next_attempt_at = NULL WHERE approved = 0")
            else:
                q = ",".join("?" * len(ids)) or "NULL"
                cursor = await db.execute(f"UPDATE sync_outbox SET approved = 1, next_attempt_at = NULL WHERE id IN ({q})", ids)
        return cursor.rowcount

    async def delete_outbox_ids(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Deletes the given rows and returns them (so the caller can restore those titles)."""
        rows = [r for r in await self.get_outbox() if r["id"] in set(ids)]
        if rows:
            q = ",".join("?" * len(rows))
            async with self.write() as db:
                await db.execute(f"DELETE FROM sync_outbox WHERE id IN ({q})", [r["id"] for r in rows])
        return rows

    async def pending_remote_ids(self, driver: str) -> set:
        """The remote ids of the titles with rows still waiting for `driver` (approved or not)."""
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT DISTINCT remote_id FROM sync_outbox WHERE driver = ? AND remote_id IS NOT NULL", (driver,))
        return {r["remote_id"] for r in await cursor.fetchall()}

    async def flagged_remote_ids(self, driver: str) -> Tuple[set, set]:
        """(keys with any row held for a full pull, those among them whose rows are ALL held): the second set is the
        titles a full pull resolves in the account's favour."""
        db = await self.read_connection()
        rows = await (await db.execute(
            f"SELECT remote_id, {_FLAG} AS f FROM sync_outbox WHERE driver = ? AND remote_id IS NOT NULL", (driver,))
        ).fetchall()
        flagged = {r["remote_id"] for r in rows if r["f"]}
        return flagged, flagged - {r["remote_id"] for r in rows if not r["f"]}

    async def has_flagged_outbox(self, driver: str) -> bool:
        db = await self.read_connection()
        return (await (await db.execute(
            f"SELECT 1 FROM sync_outbox WHERE driver = ? AND {_FLAG} = 1 LIMIT 1", (driver,))).fetchone()) is not None

    async def resolve_flagged_outbox(self, driver: str, on_account: set) -> Dict[str, int]:
        """The end of a successful full pull: `on_account` are the keys of the titles its listings showed. Rows held
        for a full pull (`if_missing`) are resolved in one transaction:
        - the account has the title: the held add is deleted (the account's state came in); later held edits are kept,
          their hold is lifted and they become UNAPPROVED, for the person to approve (send) or decline;
        - the account lacks it: the hold is lifted on every row; they go out as usual."""
        out = {"dropped": 0, "kept_for_review": 0, "released": 0}
        async with self.write() as db:
            rows = await (await db.execute(
                f"SELECT id, remote_id, payload_json FROM sync_outbox WHERE driver = ? AND {_FLAG} = 1", (driver,))
            ).fetchall()
            for r in rows:
                payload = json.loads(r["payload_json"])
                kind = payload.pop("if_missing")
                if r["remote_id"] not in on_account:
                    await db.execute("UPDATE sync_outbox SET payload_json = ? WHERE id = ?", (json.dumps(payload), r["id"]))
                    out["released"] += 1
                elif kind in ("edit",):
                    await db.execute("UPDATE sync_outbox SET payload_json = ?, approved = 0 WHERE id = ?",
                                     (json.dumps(payload), r["id"]))
                    out["kept_for_review"] += 1
                else:
                    await db.execute("DELETE FROM sync_outbox WHERE id = ?", (r["id"],))
                    out["dropped"] += 1
        return out

    async def mark_solo(self, outbox_ids: List[int]) -> None:
        """After a batch failed in a way that says nothing about whether the request went out: send each row alone
        next time (a repeated batch could send it twice)."""
        if outbox_ids:
            async with self.write() as db:
                await db.execute(
                    f"UPDATE sync_outbox SET payload_json = json_set(payload_json, '$.solo', 1) "
                    f"WHERE id IN ({','.join('?' * len(outbox_ids))})", list(outbox_ids))

    async def release_outbox_backoff(self) -> None:
        """Makes every approved outbox item eligible for an immediate retry."""
        async with self.write() as db:
            await db.execute("UPDATE sync_outbox SET next_attempt_at = NULL WHERE approved = 1")

    async def clear_outbox(self) -> int:
        async with self.write() as db:
            cursor = await db.execute("DELETE FROM sync_outbox")
        return cursor.rowcount
