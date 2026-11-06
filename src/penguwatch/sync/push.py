"""Push half of the sync engine: outbox queueing, delivery and review (approve/discard).

Rows of different titles that the library driver says may share a request (LibrarySync.batch_key) go out together,
MAX_BATCH at a time; a title's rows always go in order, and a title waits behind any older approved row that is
backing off after a failed delivery."""

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from penguwatch import telemetry
from penguwatch.drivers import DRIVERS
from penguwatch.drivers.base import LOCAL_ID, OP_REMOVE, OP_STATUS, SyncError, SyncOp
from penguwatch.models import Show, ShowStatus, MediaType
from penguwatch.sync import ops as sync_ops
from penguwatch.sync.active import LAST_ACCOUNT_KEY
from penguwatch.sync.enrich import _iso, _utcnow

logger = logging.getLogger("penguwatch.sync")

MAX_BATCH = 25  # titles per request
MAX_ROUNDS = 20  # reads of the due rows (50 each) per flush; a long queue is finished by the next one


def _backoff_seconds(attempts: int) -> int:
    return min(6 * 3600, 30 * (2 ** min(attempts, 10)))


class DiscardRefused(Exception):
    """The rows belong to a driver that is not the connected library driver; the message says what to do."""


def _summary(op: SyncOp, show: Show, library_label: str) -> str:
    """What the row says in the review. A removal names its title: the title itself is gone by the time it is shown."""
    text = sync_ops.describe(op, library_label)
    return f"{text}: {show.title}" if op.kind == OP_REMOVE else text


class PushMixin:
    # ------------------------------------------------------------------ review (outgoing)

    async def approve_outgoing(self, ids: Optional[List[int]] = None) -> int:
        n = await self.repository.approve_outbox(ids)
        self.kick.set()
        return n

    def discard_refusal(self, row: Dict[str, Any]) -> Optional[str]:
        """Why this row cannot be discarded now (None: it can). Discarding means taking the account's version, which
        needs the row's driver to be the library driver and connected."""
        if row.get("driver") == self.driver and self.library.is_connected():
            return None
        cls = DRIVERS.get(row.get("driver") or "")
        label = cls.label if cls else str(row.get("driver"))
        if row.get("driver") == self.driver:  # the library driver, only not connected
            return f"Connect {label} to decide on these."
        return f"Switch the library back to {label} and connect to decide on these."

    async def discard_outgoing(self, ids: List[int], restore: bool = True) -> Dict[str, Any]:
        """Drops queued local changes and restores those titles to what the library has. Refused (DiscardRefused,
        nothing deleted) for rows of a driver that is not the connected library driver: there is no account to
        restore from. `restore=False` only drops the rows (always allowed): for rows that cannot be declined, because
        the account to take the version from is not there."""
        wanted = set(ids)
        if not restore:  # "drop without restoring": only deletes; nothing is fetched, the local state stays as it is
            rows = await self.repository.delete_outbox_ids(ids)
            return {"discarded": len(rows), "restored": 0, "removed": 0}
        for row in await self.repository.get_outbox():
            if row["id"] in wanted and (why := self.discard_refusal(row)):
                raise DiscardRefused(why)
        rows = await self.repository.delete_outbox_ids(ids)
        still_pending = await self.repository.pending_remote_ids(self.driver)
        # key -> media type: from the title, or from the row's operation when the title is gone (a discarded removal)
        targets = {r["remote_id"]: r["operation"].media_type for r in rows
                   if r.get("remote_id") and r.get("driver") == self.driver and r["remote_id"] not in still_pending}
        restored = removed = 0
        if targets and self.library.is_connected():
            provider = self.library.key_provider
            by_media: Dict[str, List[Any]] = {}
            for key, media in targets.items():
                show = await self.repository.get_show_by_external_id(provider, key)
                by_media.setdefault(show.media_type.value if show else media, []).append(key)
            for media, keys in by_media.items():
                found = {c.ids.get(provider): c for c in await self.library.entries(media)}
                for key in keys:
                    if key in found:
                        await self._apply_change(found[key], set())
                        restored += 1
                    else:  # was only ever added here -> undo the add
                        show = await self.repository.get_show_by_external_id(provider, key)
                        if show:
                            await self.repository.delete_show(show.id)
                            removed += 1
        return {"discarded": len(rows), "restored": restored, "removed": removed}

    # ------------------------------------------------------------------ push (outbox)

    async def review_mode(self) -> bool:
        """When on (default), local changes wait for approval and library changes are staged for review. Always off
        for a library without an account: there is nothing to approve."""
        if not self.library.remote:
            return False
        return (await self.repository.get_state("sync_review_mode", "on")) != "off"

    async def _queue(self, op: SyncOp, show: Show, summary: Optional[str] = None,
                     force_approved: bool = False, new_title: bool = False) -> bool:
        if not self.library.remote:
            return await self._queue_for_account(op, show)  # no account behind the library now
        key = show.ids.get(self.library.key_provider)
        if not key:
            return False  # not in the library's catalogue: nothing to push
        approved = force_approved or not await self.review_mode()
        if new_title and op.kind == OP_STATUS and not self.library.is_connected():
            # A title added here while the account is not connected may already be on the account: "add if missing",
            # held until a full pull shows what the account has (sync.pull). Edits of titles already here are not
            # flagged: they go out as usual once connected, so an edit made while disconnected is never dropped.
            op.if_missing = "add"
        elif key in (await self.repository.flagged_remote_ids(self.driver))[0]:
            # An edit of such a title before that pull belongs with its add: sent with it if the account lacks the title,
            # kept for the person to decide on (unapproved) if the account has it.
            op.if_missing = "edit"
        await self.repository.enqueue_outbox(self.driver, op, show.id, key,
                                             summary=summary or _summary(op, show, self.library.label),
                                             approved=approved)
        if approved:
            self.kick.set()
        return True

    async def account_key(self, show: Show) -> Any:
        """The id the title's changes are queued under (None: its changes are not queued anywhere): the library
        driver's id, or, while the library is local, the last account driver's (see _queue_for_account)."""
        if self.library.remote:
            return show.ids.get(self.library.key_provider)
        if LOCAL_ID in show.ids:
            return None
        cls = DRIVERS.get(await self.repository.get_state(LAST_ACCOUNT_KEY) or "")
        if cls is None or not getattr(cls, "remote", False):
            return None
        return show.ids.get(cls.key_provider)

    async def _queue_for_account(self, op: SyncOp, show: Show) -> bool:
        """The library has no account: keep the edit for the last account driver, so that going back to it does not
        undo the edit.

        The row is written for that driver and never approved, whatever the review-mode setting: nothing is sent while
        local, and after switching back the person decides in the outgoing review (approve: it goes out; discard: the
        account's version comes back). It also marks the title as having an unsent change, so a pull leaves it alone.
        Nothing is queued for a title the account does not know (no id of that driver's), for a title added while
        local (it was never on the account: a discarded row would delete it), or when no account driver ever ran."""
        if LOCAL_ID in show.ids:
            return False
        name = await self.repository.get_state(LAST_ACCOUNT_KEY)
        cls = DRIVERS.get(name or "")
        if cls is None or not getattr(cls, "remote", False) or name == self.driver:
            return False
        key = show.ids.get(cls.key_provider)
        if not key:
            return False
        await self.repository.enqueue_outbox(name, op, show.id, key, summary=_summary(op, show, cls.label),
                                             approved=False)
        return True

    def _batch_key(self, row: Dict[str, Any]) -> Tuple[Any, Any]:
        """Rows with equal keys can share one request. Ops the driver does not batch never share."""
        key = self.library.batch_key(row["operation"])
        if key is None or row["payload"].get("solo"):  # solo: a batch of it failed in a way that may have sent it
            return ("single", row["id"])
        return ("batch", key)

    async def _stop_on(self, e: SyncError) -> bool:
        """Login problems end the whole flush (and record why); returns True when handled."""
        if e.kind == "scope":
            await self.set_last_error(e.message)
            return True
        if e.kind == "auth":
            await self._auth_failed(e)
            return True
        return False

    async def _back_off(self, row: Dict[str, Any], error: str) -> None:
        attempts = (row.get("attempts") or 0) + 1
        await self.repository.fail_outbox(row["id"], error, _iso(_utcnow() + timedelta(seconds=_backoff_seconds(attempts))))

    async def _send_one(self, row: Dict[str, Any], blocked: set, stats: Optional[Dict[str, int]] = None) -> str:
        """Sends one outbox row. Returns "sent", "dropped" (duplicate or given up on: row deleted, not counted),
        "kept" (failed, backing off; its title is added to `blocked`) or "stop" (a login problem)."""
        key = row.get("remote_id")
        try:
            if stats is not None:
                stats["requests"] += 1
            await self.library.push([row["operation"]])
        except SyncError as e:
            if await self._stop_on(e):
                return "stop"
            if e.kind == "duplicate":  # already recorded there
                await self.repository.delete_outbox(row["id"])
                return "dropped"
            attempts = (row.get("attempts") or 0) + 1
            if e.permanent and attempts >= 3:
                logger.error(f"Dropping {self.library.label} push {row['op']} for {key} after {attempts} attempts: {e}")
                await self.repository.delete_outbox(row["id"])
                return "dropped"
            await self._back_off(row, str(e))
            blocked.add(key)
            return "kept"
        except Exception as e:  # a driver bug or an error it did not map: like a transient failure, never a blocked outbox
            logger.error(f"{self.library.label} push {row['op']} for {key} raised {type(e).__name__}: {e}", exc_info=True)
            await self._back_off(row, f"{type(e).__name__}: {e}")
            blocked.add(key)
            return "kept"
        await self.repository.delete_outbox(row["id"])
        return "sent"

    async def _send_each(self, rows: List[Dict[str, Any]], blocked: set, stats: Dict[str, int]) -> List[str]:
        results: List[str] = []
        for i, row in enumerate(sorted(rows, key=lambda r: r["id"])):
            res = await self._send_one(row, blocked, stats)
            results.append(res)
            if res == "stop":
                results.extend(["stop"] * (len(rows) - i - 1))
                break
        return results

    @telemetry.traced("sync.push", lambda self, rows, *a, **k: {"op": rows[0]["op"], "rows": len(rows)})
    async def _send_batch(self, rows: List[Dict[str, Any]], blocked: set, stats: Dict[str, int]) -> List[str]:
        """Sends rows that share a batch key (and belong to different titles) as one request.
        Returns one result per row, in the order given (same words as _send_one)."""
        if len(rows) == 1:
            return [await self._send_one(rows[0], blocked, stats)]
        try:
            await self.library.push([r["operation"] for r in rows])
        except SyncError as e:
            if e.kind == "unbatchable":
                logger.warning(f"Cannot merge {len(rows)} {self.library.label} pushes ({e}); sending them one by one.")
                return await self._send_each(rows, blocked, stats)
            stats["requests"] += 1
            if await self._stop_on(e):
                return ["stop"] * len(rows)
            if e.kind == "transient":
                # One-by-one retries would each wait out the same pause: back everything off instead.
                for r in rows:
                    await self._back_off(r, str(e))
                    blocked.add(r.get("remote_id"))
                return ["kept"] * len(rows)
            logger.warning(f"{self.library.label} rejected a batch of {len(rows)} {rows[0]['op']} pushes ({e}); "
                           "sending them one by one.")
            return await self._send_each(rows, blocked, stats)
        except Exception as e:
            # Not a SyncError: nothing says whether the request went out before this was raised, so do not resend
            # now. Back every row off like a transient error and send each alone on its next try.
            logger.error(f"{self.library.label} push of {len(rows)} {rows[0]['op']} rows raised "
                         f"{type(e).__name__}: {e}; backing them off, to be sent one at a time.", exc_info=True)
            stats["requests"] += 1
            await self.repository.mark_solo([r["id"] for r in rows])
            for r in rows:
                await self._back_off(r, f"{type(e).__name__}: {e}")
                blocked.add(r.get("remote_id"))
            return ["kept"] * len(rows)
        stats["requests"] += 1
        await self.repository.delete_outbox_many([r["id"] for r in rows])
        return ["sent"] * len(rows)

    async def flush_outbox(self) -> int:
        """Delivers queued changes. Rows still waiting for approval are never sent and never hold approved ones
        back."""
        if not self.library.remote or not self.library.is_connected():
            return 0
        sent = 0
        stats = {"requests": 0}
        async with self._push_lock:
            blocked: set = set()
            confirmed = False
            after_id = 0
            for _ in range(MAX_ROUNDS):
                # Re-read every round: a row that failed in the last one is now a hold point, and its title's later
                # rows must wait behind it. The hold points are read before the due rows: a row that becomes due in
                # between stays held.
                hold = await self.repository.outbox_hold_points(self.driver)
                due = await self.repository.get_due_outbox(self.driver, after_id=after_id)
                if not due:
                    break
                after_id = due[-1]["id"]  # the next round reads on from here, past rows that stay held or blocked
                queues: Dict[Any, List[Dict[str, Any]]] = {}
                for row in due:
                    key = row.get("remote_id")
                    if row["id"] < hold.get(key, float("inf")):
                        queues.setdefault(key, []).append(row)
                stopped = False
                while True:
                    heads = sorted((q[0] for key, q in queues.items() if q and key not in blocked), key=lambda r: r["id"])
                    if not heads:
                        break
                    batch = self._batch_key(heads[0])
                    group = [r for r in heads if self._batch_key(r) == batch][:MAX_BATCH]
                    results = await self._send_batch(group, blocked, stats)
                    for row, res in zip(group, results, strict=True):
                        key = row.get("remote_id")
                        if res in ("sent", "dropped"):
                            queues[key].pop(0)
                            sent += res == "sent"
                            if res == "sent" and not confirmed:
                                confirmed = True  # a 2xx: the login works (before any later failure may say otherwise)
                                await self.library.confirmed()
                        else:
                            blocked.add(key)
                    if "stop" in results:
                        stopped = True
                        break
                if stopped:
                    break
        if sent:
            telemetry.SYNC_CHANGES.add(sent, {"direction": "pushed", "driver": self.driver})
            logger.info(f"Pushed {sent} change(s) to {self.library.label} in {stats['requests']} request(s).")
        return sent

    async def queue_list_move(self, show: Show, status: ShowStatus, summary: Optional[str] = None,
                              force_approved: bool = False, new_title: bool = False) -> bool:
        """Queues the title's list status. `new_title`: the title was just added here (see _queue: add if missing)."""
        return await self._queue(sync_ops.status_op(show, status.value), show, summary=summary,
                                 force_approved=force_approved, new_title=new_title)

    async def remove_from_library(self, show: Show, force_approved: bool = False) -> bool:
        """Removes the title from the library's lists (and its watch history)."""
        return await self._queue(sync_ops.remove_op(show), show, force_approved=force_approved)


__all__ = ["PushMixin", "MAX_BATCH", "MediaType"]
