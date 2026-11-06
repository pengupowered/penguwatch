"""The background sync worker: pushes queued changes within seconds and pulls what changed, and its component."""

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from penguwatch import activity, clock, telemetry
from penguwatch.activity import ActivityTracker
from penguwatch.components import Component, Context
from penguwatch.sync.engine import SyncEngine

logger = logging.getLogger("penguwatch")


NOTIFIED_KEEP_DAYS = 60  # a "notified:<key>" marker only stops the same event being sent twice
HOUSEKEEPING_EVERY_S = 24 * 3600
NOT_CONNECTED_WAIT_S = 60.0  # nothing to push or pull without a login; connecting kicks the worker (api/drivers.py)


async def housekeeping(repo) -> int:
    """The daily tidying: drops the notification markers older than NOTIFIED_KEEP_DAYS. Returns how many went."""
    cutoff = (clock.utc_now() - timedelta(days=NOTIFIED_KEEP_DAYS)).isoformat()
    return await repo.delete_states_before("notified:", cutoff)


async def worker_step(engine: SyncEngine, pull_due: bool) -> str:
    """One pass of the background work: "paused", "pulled" (push + pull) or "flushed" (push only).

    While the library needs a reconnect and the login cannot renew itself, pushing and pulling would only collect
    refusals while the banner says sync is paused; the worker then waits, apart from one attempt an hour so that a
    login that works again clears the banner. Queued changes stay in the outbox."""
    gate = await engine.reconnect_gate()
    if gate == "paused":
        return "paused"
    if pull_due or gate == "probe":  # the hourly probe must reach the service even when only a push was due
        await engine.sync()
        return "pulled"
    await engine.flush_outbox()
    return "flushed"


async def sync_worker(engine: SyncEngine, interval_minutes: int,
                      idle_interval_minutes: Optional[int] = None, tracker: Optional[ActivityTracker] = None):
    """Background loop.

    * Delivers queued local changes within seconds (woken by engine.kick), retrying failed pushes with backoff.
      Unaffected by idleness.
    * Every `interval_minutes`, asks the library driver what changed and pulls only that; after a long time without
      user interaction only every `idle_interval_minutes`, and promptly once use resumes.
    """
    idle_interval = idle_interval_minutes if idle_interval_minutes is not None else interval_minutes
    telemetry.in_worker.set(True)  # this task's upstream calls count as worker heartbeats (a long sync is not "stuck")
    tracker = tracker or activity.tracker
    label = engine.library.label
    if engine.library.remote:
        logger.info(f"{label} sync worker started (pull every {interval_minutes} min, {idle_interval} min when idle, "
                    f"push on change).")
    else:
        logger.info(f"{label} library has no account: the worker only keeps titles' details up to date "
                    f"(every {interval_minutes} min, {idle_interval} min when idle).")
    await asyncio.sleep(5)
    loop = asyncio.get_running_loop()
    next_pull = loop.time()
    next_housekeeping = loop.time()
    while True:
        try:
            telemetry.worker_beat()
            if loop.time() >= next_housekeeping:
                next_housekeeping = loop.time() + HOUSEKEEPING_EVERY_S
                try:
                    pruned = await housekeeping(engine.repository)
                    if pruned:
                        logger.info(f"Housekeeping: dropped {pruned} old notification marker(s).")
                except Exception as e:  # never stops the worker
                    logger.warning(f"Housekeeping failed: {e}")
            if tracker.take_resumed():
                next_pull = loop.time()
            if engine.library.is_connected():
                if await engine.library.maintain() == "renewed":
                    logger.info(f"{label} login renewed ahead of expiry.")
                step = await worker_step(engine, loop.time() >= next_pull)
                telemetry.worker_beat(step)
                if step == "pulled":
                    minutes = activity.pick_interval_minutes(tracker.is_idle(), interval_minutes, idle_interval)
                    next_pull = loop.time() + minutes * 60
                elif step == "paused":
                    next_pull = max(next_pull, loop.time() + 60)  # nothing to do until the hourly probe or a reconnect
            timeout = max(5.0, min(120.0, next_pull - loop.time()))
            if not engine.library.is_connected():
                timeout = NOT_CONNECTED_WAIT_S
            try:
                await asyncio.wait_for(engine.kick.wait(), timeout=timeout)
                await asyncio.sleep(2)  # let quick successive clicks batch up
            except asyncio.TimeoutError:
                pass
            engine.kick.clear()
        except asyncio.CancelledError:
            logger.info(f"{label} sync worker stopped.")
            break
        except Exception as e:
            telemetry.worker_beat("error")
            logger.error(f"Error in the {label} sync worker: {e}", exc_info=True)
            await asyncio.sleep(60)


async def register(ctx: Context) -> Component:
    engine = ctx.sync
    library = engine.library

    async def start() -> None:
        # A freshly started container counts as active; use after an idle spell wakes the worker at once.
        activity.tracker.idle_after_seconds = library.idle_after_hours * 3600
        activity.tracker.on_resume = engine.kick.set
        activity.tracker.touch()
        ctx.spawn(sync_worker(engine, library.pull_interval_minutes, library.idle_pull_interval_minutes,
                              activity.tracker))

    async def stop() -> None:
        # One last attempt to deliver queued changes before the drivers close
        try:
            await asyncio.wait_for(engine.flush_outbox(), timeout=10)
        except Exception:
            pass

    return Component("sync worker", start=start, stop=stop)
