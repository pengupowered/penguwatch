"""Metadata enrichment and refresh from the catalogue chain: details, sequels, episode lists, the schedule.

`EnrichMixin` is mixed into `SyncEngine`; it relies on `self.catalogs` (CatalogProviders, first that knows a title
wins), `self.schedules` and `self.repository`.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from penguwatch import clock
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.sync import backoff
from penguwatch.sync.titles import merge_title, sequel_record, stored_ratings, title_names

logger = logging.getLogger("penguwatch.sync")

# Pause between consecutive catalogue requests of one background batch (read at call time, so tests can zero it).
CALL_SPACING_S = 0.5

REQUEST_DEADLINE_S = 3.0  # how long a request waits for a catalogue before it answers from what is stored
_REQUEST_DEADLINE = object()  # "the default deadline" (None means no deadline)
_LATE_REFRESHES: set = set()  # fetches that outlived their request; kept so they are not collected half-way


def _late_refresh_done(task) -> None:
    _LATE_REFRESHES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.debug(f"Late episode list refresh failed: {task.exception()}")


def _utcnow() -> datetime:
    return clock.utc_now()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _older_than(ts: Optional[str], delta: timedelta, now: Optional[datetime] = None) -> bool:
    """True when `ts` (ISO) is missing, unreadable or older than `delta` (against this module's clock: the one the
    *_synced_at / *_at stamps are written with)."""
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now or _utcnow()) - dt > delta
    except ValueError:
        return True


async def _paced(items: Iterable[Any], work: Callable[[Any], Awaitable[None]], pace: bool = True) -> None:
    """Runs `work(item)` one item at a time, sleeping CALL_SPACING_S between items (not after the last)."""
    for i, item in enumerate(items):
        if i and pace:
            await asyncio.sleep(CALL_SPACING_S)
        await work(item)


class EnrichMixin:
    def catalog_for(self, show: Show):
        """The catalogue that looks the title up: the library driver's own, when the title has its id and it is in the
        chain (its episode numbering is the one the library's pushes use), else the first catalogue the title has an id
        for (None: no catalogue knows it)."""
        own = self.library.key_provider
        if show.ids.get(own):
            for catalog in self.catalogs:
                if catalog.key_provider == own:
                    return catalog
        for catalog in self.catalogs:
            if show.ids.get(catalog.key_provider):
                return catalog
        return None

    def _catalog_providers(self) -> List[str]:
        return [c.key_provider for c in self.catalogs]

    async def enrich_titles(self, show_ids: Optional[Iterable[int]] = None, limit: int = 25, pace: bool = True) -> int:
        """Fills network / ratings / artwork from the catalogue's details for new or stale titles.

        Details are fetched one title at a time, CALL_SPACING_S apart; pass pace=False when a person is waiting on
        the result (accepting incoming changes)."""
        background = show_ids is None
        attempts: Dict[str, str] = {}
        if not background:  # asked for by a person or an import: never subject to the failure back-off
            targets = [s for s in [await self.repository.get_show_by_id(i) for i in show_ids] if s]
            targets = [s for s in targets if self.catalog_for(s) and (not s.enriched_at or not s.network)]
        else:
            attempts = await self._load_attempts()
            targets = await self._background_targets(limit, attempts)
        targets = targets[:limit]
        if not targets:
            return 0

        async def run(s: Show) -> None:
            try:
                await self._enrich_one(s)
            except Exception as e:
                logger.debug(f"Enrichment of {s.title} failed: {e}")

        if background:
            await self._run_tracked(targets, "enrich", attempts, self._enrich_one, pace)
        else:
            await _paced(targets, run, pace)
        return len(targets)

    async def _background_targets(self, limit: int, attempts: Dict[str, str]) -> List[Show]:
        """Titles due for details: stale ones (14 days) first, then those whose alternative titles were never
        fetched (titles being watched first, so Up Next fills soonest; anime before others), all subject to the
        back-off. Logs how many of the latter wait for a later cycle."""
        unchecked = await self.repository.get_unchecked_title_ids(self._catalog_providers())
        shows = [s for s in await self.repository.get_shows() if self.catalog_for(s)]
        stale = [s for s in shows if _older_than(s.enriched_at, timedelta(days=14))]
        stale_ids = {s.id for s in stale}
        backfill = sorted((s for s in shows if s.id in unchecked and s.id not in stale_ids),
                          key=lambda s: (s.status != ShowStatus.WATCHING, s.media_type != MediaType.ANIME))
        due = self._not_backed_off(stale, "enrich", attempts) + self._not_backed_off(backfill, "enrich", attempts)
        waiting = len(unchecked) - sum(1 for s in due[:limit] if s.id in unchecked)
        if waiting > 0:
            logger.info(f"English/alternative title backfill: {waiting} titles still to fetch")
        return due

    async def _load_attempts(self) -> Dict[str, str]:
        return backoff.load(await self.repository.get_state(backoff.STATE_KEY))

    @staticmethod
    def _not_backed_off(shows: List[Show], kind: str, attempts: Dict[str, str]) -> List[Show]:
        now = _utcnow()
        return [s for s in shows if backoff.due(attempts, f"{kind}:{s.id}", now)]

    async def _run_tracked(self, targets: List[Show], kind: str, attempts: Dict[str, str],
                           work: Callable[[Show], Awaitable[bool]], pace: bool = True) -> None:
        """Runs `work` per title (paced); a False result or an exception starts a 24 h back-off for that
        title, success clears it. The attempts dict is written once, at the end, and only if it changed."""
        before = dict(attempts)

        async def one(s: Show) -> None:
            key = f"{kind}:{s.id}"
            try:
                ok = bool(await work(s))
            except Exception as e:
                logger.debug(f"{kind} refresh of {s.title} failed: {e}")
                ok = False
            if ok:
                attempts.pop(key, None)
            else:
                attempts.update(backoff.record(attempts, key, _utcnow()))

        try:
            await _paced(targets, one, pace)
        finally:
            pruned = backoff.prune(attempts, _utcnow())
            if pruned != before:
                if pruned:
                    await self.repository.set_state(backoff.STATE_KEY, backoff.dump(pruned))
                else:
                    await self.repository.delete_state(backoff.STATE_KEY)

    async def _enrich_one(self, show: Show) -> bool:
        """Stamps and stores what the catalogue knows; False when it returned nothing for the title."""
        fields: Dict[str, Any] = {"enriched_at": _iso(_utcnow())}
        title = await self._fetch_title(show)
        fields.update(merge_title(show, title))
        await self._persist_enrichment(show, fields)
        await self.store_ratings(show, title)
        names = title_names(show, title)
        if names is not None:
            await self.repository.update_show_titles(show.id, *names)
        return title is not None

    async def store_ratings(self, show: Show, title) -> None:
        """Built-in rating sources without a column go to title_ratings (quietly: no rating event for a refresh).
        Only to a source whose row is built-in: a custom source with the same id is never written to."""
        ratings = stored_ratings(title) if title is not None else {}
        if not ratings:
            return
        builtin = await self.repository.builtin_rating_source_ids()
        for source, value in ratings.items():
            if source not in builtin:  # not set up (its driver is not in config.yaml's drivers:), or a custom source
                logger.debug(f"Rating {source} of {show.title} not stored: no built-in source of that id")
                continue
            try:
                await self.repository.set_title_ratings(source, {show.id: value}, events="none")
            except Exception as e:
                logger.debug(f"Rating {source} of {show.title} not stored: {e}")

    async def _fetch_title(self, show: Show):
        """The catalogue's details of the title, or None when the lookup fails."""
        catalog = self.catalog_for(show)
        if catalog is None:
            return None
        try:
            return await catalog.title(show.media_type.value, show.ids)
        except Exception as e:
            logger.debug(f"Details for {show.title} failed: {e}")
            return None

    async def _persist_enrichment(self, show: Show, fields: Dict[str, Any]) -> None:
        """Auto-matches the streaming service (unless set by hand) and writes the fields."""
        if not show.service_manual and not show.service_id:
            network = fields.get("network") or show.network
            sid = await self.repository.match_service_by_network(network, title=show.title)
            if not sid and show.media_type == MediaType.ANIME:
                sid = await self.repository.default_anime_service()
                if sid:
                    fields.setdefault("network", network or "Crunchyroll")
            if sid:
                fields["service_id"] = sid
        await self.repository.update_show_fields(show.id, fields)

    async def refresh_sequels(self, limit: int = 25) -> int:
        """Checks completed anime for sequels (a catalogue may list new seasons as separate titles)."""
        attempts = await self._load_attempts()
        targets = self._not_backed_off([
            s for s in await self.repository.get_shows(status=ShowStatus.COMPLETED, media_type=MediaType.ANIME)
            if self.catalog_for(s) and (s.sequels is None or _older_than(s.enriched_at, timedelta(days=7)))
        ], "sequels", attempts)[:limit]

        async def run(s: Show) -> bool:
            related = await self.catalog_for(s).related(s.media_type.value, s.ids)  # raising = failed attempt
            if related is None:
                return False
            await self.repository.update_show_fields(
                s.id, {"sequels_json": json.dumps([sequel_record(r) for r in related]), "enriched_at": _iso(_utcnow())}
            )
            return True

        await self._run_tracked(targets, "sequels", attempts, run)
        return len(targets)

    async def _refresh_show_episodes(self, show: Show) -> bool:
        catalog = self.catalog_for(show)
        if catalog is None:
            return False
        episodes = await catalog.episodes(show.media_type.value, show.ids) or []
        eps = [{"show_id": show.id, "ids": e.ids, "season": e.season, "number": e.number, "title": e.title,
                "air_date": e.air_date} for e in episodes]
        if not eps:
            return False
        await self.repository.upsert_episode_list(show.id, eps)
        # Libraries created by older versions only have counts: seed per-episode flags in airing order
        existing = await self.repository.get_episodes(show.id)
        if show.watched_episodes > 0 and not any(e.is_watched for e in existing):
            ordered = [e for e in existing if e.season > 0]
            await self.repository.set_episodes_watched(
                show.id, [(e.season, e.number) for e in ordered[: show.watched_episodes]], True, watched_at=show.last_watched_at
            )
        await self.repository.update_show_fields(show.id, {"episodes_synced_at": _iso(_utcnow())})
        await self.repository.recompute_progress_from_episodes(show.id)
        return True

    async def ensure_episode_list(self, show: Show, max_age: timedelta = timedelta(hours=12),
                                  deadline: Any = _REQUEST_DEADLINE) -> bool:
        """Makes sure we know the show's real episode list (needed to push exact season/episode).

        A person is waiting for this (a click, the episode checklist), so the catalogue gets `deadline` seconds
        (REQUEST_DEADLINE_S; None waits for it, as background callers may). When it is slower the stored list is used
        and the fetch carries on in the background, so the next look has it."""
        if show.media_type == MediaType.MOVIE or not self.catalog_for(show):
            return False
        if show.episodes_synced_at and not _older_than(show.episodes_synced_at, max_age):
            return True
        if deadline is _REQUEST_DEADLINE:
            deadline = REQUEST_DEADLINE_S
        try:
            if deadline is None:
                return await self._refresh_show_episodes(show)
            task = asyncio.ensure_future(self._refresh_show_episodes(show))
            done, _ = await asyncio.wait({task}, timeout=deadline)
            if not done:
                logger.debug(f"Episode list for {show.title} is slow; using the stored one while it loads.")
                _LATE_REFRESHES.add(task)
                task.add_done_callback(_late_refresh_done)
                return bool(show.episodes_synced_at)
            return task.result()
        except Exception as e:
            logger.debug(f"Episode list refresh failed for {show.title}: {e}")
            return bool(show.episodes_synced_at)

    async def refresh_episode_lists(self, limit: int = 25) -> int:
        shows = await self.repository.get_shows()
        targets = []
        for s in shows:
            if s.media_type == MediaType.MOVIE or not self.catalog_for(s):
                continue
            if s.status == ShowStatus.WATCHING and _older_than(s.episodes_synced_at, timedelta(hours=20)):
                targets.append(s)
            elif s.status in (ShowStatus.PLANTOWATCH, ShowStatus.HOLD) and _older_than(s.episodes_synced_at, timedelta(days=3)):
                targets.append(s)
            elif s.status == ShowStatus.COMPLETED and not s.is_ended and _older_than(s.episodes_synced_at, timedelta(days=4)):
                # Completed but still running: watch for new seasons
                targets.append(s)
        order = {ShowStatus.WATCHING: 0, ShowStatus.PLANTOWATCH: 1, ShowStatus.HOLD: 1, ShowStatus.COMPLETED: 2}
        targets.sort(key=lambda s: order.get(s.status, 3))
        attempts = await self._load_attempts()
        targets = self._not_backed_off(targets, "episodes", attempts)[:limit]
        await self._run_tracked(targets, "episodes", attempts, self._refresh_show_episodes)
        return len(targets)

    async def sync_upcoming_schedule(self) -> int:
        """Upcoming episodes from the schedule providers for titles not covered by a full episode list."""
        try:
            entries = []
            for schedule in self.schedules:
                entries.extend(await schedule.upcoming())
            known: Dict[str, Dict[Any, int]] = {}  # provider -> {id: show id}: the lists cover far more than the library
            touched = set()
            for e in entries:
                if not e.ids:
                    continue
                provider, ext = next(iter(e.ids.items()))
                if provider not in known:
                    known[provider] = await self.repository.get_external_ids(provider)
                if ext not in known[provider]:
                    continue
                show = await self.repository.get_show_by_id(known[provider][ext])
                if not show or show.episodes_synced_at:
                    continue  # full episode list already gives exact dates
                await self.repository.upsert_episode_list(
                    show.id, [{"season": e.season, "number": e.number, "title": e.title, "air_date": e.air_date}]
                )
                touched.add(show.id)
                if e.air_date >= clock.today().isoformat() and (
                    not show.next_episode_air_date or e.air_date < show.next_episode_air_date
                    or show.next_episode_air_date < clock.today().isoformat()
                ):
                    await self.repository.update_show_fields(show.id, {
                        "next_episode_air_date": e.air_date, "next_episode_season": e.season,
                        "next_episode_number": e.number, "next_episode_title": e.title,
                    })
            await self.repository.set_state("schedule_last_sync", _iso(_utcnow()))
            return len(touched)
        except Exception as e:
            logger.warning(f"Error updating the upcoming schedule: {e}")
            return 0
