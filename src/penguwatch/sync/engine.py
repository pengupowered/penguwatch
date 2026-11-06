"""The sync engine: two-way sync with the active library driver.

A library driver without an account (`remote = False`, drivers.base) has nothing to sync: the engine then pushes and
pulls nothing, and a run only does the upkeep below (details, episode lists, the schedule). Edits made meanwhile to
titles of the last account driver are queued for it, unapproved (push.py), so switching back does not undo them.

Pull (library -> PenguWatch): the driver plans the pull from the stored cursor and yields the changes a part at a
time; the engine applies (or, in review mode, stages) them. The new cursor is saved only once every part was
applied, so a pull that fails half-way is simply redone.

Push (PenguWatch -> library) goes through a persistent outbox of neutral operations: every local change (episodes
watched or unwatched, status, rating, add, remove) is queued and delivered by a background worker with retries.
Until a change has been delivered, pulls never overwrite that title's local watch state, so nothing is lost or
"un-done" by a sync that races a local edit.
"""

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional, Sequence

from penguwatch import telemetry
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.base import LOCAL_ID, SyncError, state_key
from penguwatch.observability import redact
from penguwatch.sync.actions import ActionsMixin
from penguwatch.sync.enrich import EnrichMixin, _iso, _older_than, _utcnow
from penguwatch.sync.pull import PullMixin
from penguwatch.sync.push import PushMixin

logger = logging.getLogger("penguwatch.sync")

RECONNECT_PROBE_SECONDS = 3600  # while a reconnect is needed, the worker tries the service again only this often


class SyncEngine(PullMixin, EnrichMixin, PushMixin, ActionsMixin):
    """Keeps the library in step with `library` (a LibrarySync), filling titles from `catalogs` (CatalogProviders,
    tried in order) and upcoming episodes from `schedules`."""

    def __init__(self, library, repository: DatabaseRepository, catalogs: Sequence = (), schedules: Sequence = ()):
        self.library = library
        self.repository = repository
        self.catalogs = list(catalogs)
        self.schedules = list(schedules)
        self._sync_lock = asyncio.Lock()
        self._push_lock = asyncio.Lock()
        self.kick = asyncio.Event()  # set when the outbox has new work
        self.last_result: Dict[str, Any] = {}
        self.on_run = None  # callback(trigger, started_unix, result) for telemetry
        self._last_probe: Optional[float] = None  # time.monotonic() of the last attempt while a reconnect is needed

    # ------------------------------------------------------------------ the library driver's state

    @property
    def driver(self) -> str:
        return self.library.name

    def state_key(self, key: str) -> str:
        return state_key(self.library.name, key)

    def new_title_ids(self) -> Dict[str, Any]:
        """The ids a title gets when it is added: none for a library with an account (the catalogue's ids identify
        it there), a `local` id for one without, which marks it as never having been on an account."""
        return {} if self.library.remote else {LOCAL_ID: uuid.uuid4().hex}

    async def get_state(self, key: str) -> Optional[str]:
        return await self.repository.get_state(self.state_key(key))

    async def set_last_error(self, message: Optional[str]) -> None:
        if message is None:
            await self.repository.delete_state(self.state_key("last_error"))
        else:
            await self.repository.set_state(self.state_key("last_error"), message)

    # ------------------------------------------------------------------ orchestration

    async def reconnect_gate(self) -> str:
        """What the background worker may do: "open" (carry on as usual), "paused" or "probe".

        "paused" while the login needs a person (LibrarySync.needs_reconnect), because every push and pull would only
        collect another refusal. One attempt per hour still goes through: this call claims it and returns "probe", and
        the caller must then really contact the service (a full sync()), so a login that works again clears the
        banner by itself. Queued rows are not touched; manual syncs are not affected."""
        if not await self.library.needs_reconnect():
            return "open"
        now = time.monotonic()
        if self._last_probe is None or now - self._last_probe >= RECONNECT_PROBE_SECONDS:
            self._last_probe = now
            return "probe"
        return "paused"

    async def _auth_failed(self, error: SyncError) -> str:
        msg = await self.library.on_auth_error(error)
        self._last_probe = time.monotonic()
        return msg

    @telemetry.traced("sync.run", lambda self, *a, trigger="scheduled", **k: {"trigger": trigger})
    async def sync(self, force_full: bool = False, deep: bool = True, trigger: str = "scheduled") -> Dict[str, Any]:
        """Push pending changes, then pull what changed in the library, then refresh metadata."""
        if not self.library.is_connected():
            return {"status": "skipped", "reason": "not_authenticated"}
        async with self._sync_lock:
            started = _utcnow()
            result: Dict[str, Any] = {"status": "success"}
            previous_error = await self.get_state("last_error")
            # A first full sync (nothing synced before) would raise an event per title: one summary instead
            first_sync = self.library.remote and not await self.get_state("last_sync")
            titles_before = await self.repository.count_titles() if first_sync else 0
            if first_sync:
                self.repository.mute_events()
            try:
                if self.library.remote:
                    result["pushed"] = await self.flush_outbox()
                    pull = await self._pull(force_full)
                else:
                    result["pushed"] = 0
                    pull = {"mode": "none", "updated": 0, "removed": 0, "touched_ids": []}
                result.update({k: v for k, v in pull.items() if k != "touched_ids"})
                if deep:
                    touched = pull.get("touched_ids") or []
                    if touched:
                        await self.enrich_titles(touched, limit=500 if pull["mode"] == "full" else 80)
                    result["enriched"] = await self.enrich_titles(limit=25)
                    result["episode_lists"] = await self.refresh_episode_lists()
                    result["sequel_checks"] = await self.refresh_sequels()
                    if _older_than(await self.repository.get_state("schedule_last_sync"), timedelta(hours=6)):
                        result["schedule"] = await self.sync_upcoming_schedule()
                await self.set_last_error(None)
            except SyncError as e:
                if e.kind == "auth":
                    msg = await self._auth_failed(e)
                else:
                    msg = e.message[:300]
                    await self.set_last_error(msg)
                result = {"status": "error", "error": msg}
            except Exception as e:
                logger.error(f"{self.library.label} sync failed: {e}", exc_info=True)
                await self.set_last_error(str(e)[:300])
                result = {"status": "error", "error": str(e)[:300]}
            except BaseException:  # cancelled at shutdown: do not leave the events muted
                if first_sync:
                    self.repository.unmute_events()
                raise
            if first_sync:
                self.repository.unmute_events()
                added = await self.repository.count_titles() - titles_before
                if added > 0:
                    self.repository.emit("library.imported", {"count": added})
            outbox = await self.repository.get_outbox()
            result["awaiting_approval"] = sum(1 for o in outbox if not o["approved"])
            now = _iso(_utcnow())
            if result.get("status") == "success":
                await self.repository.set_state(self.state_key("last_sync"), now)
            result["timestamp"] = now
            result["duration_s"] = round((_utcnow() - started).total_seconds(), 1)
            result["synced_shows"] = result.get("updated", 0)
            self.last_result = result
            if result.get("status") == "error" and result.get("error") != previous_error:  # a new failure, not a retry
                self.repository.emit("sync.failed", {"error": redact(str(result.get("error") or ""))[:300],
                                                     "trigger": trigger, "at": now})
            if self.on_run:
                try:
                    self.on_run(trigger, started.timestamp(), result)
                except Exception:
                    pass
            logger.info(f"{self.library.label} sync finished: {result}")
            return result
