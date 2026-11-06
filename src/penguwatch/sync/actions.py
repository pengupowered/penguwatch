"""Local edits of the sync engine: watched episodes, seasons, status and rating (each queued for push)."""

from typing import Dict, Any, Optional, List, Tuple, Iterable

from penguwatch import clock
from penguwatch.db.episodes import format_episode_code, parse_episode_code
from penguwatch.models import Show, ShowStatus, MediaType
from penguwatch.sync import ops as sync_ops
from penguwatch.sync.enrich import _iso, _utcnow


class ActionsMixin:
    # ------------------------------------------------------------------ local edits (+ push)

    async def _ordered_aired(self, show_id: int):
        today = clock.today().isoformat()
        eps = [e for e in await self.repository.get_episodes(show_id) if e.season > 0]
        return [e for e in eps if (e.air_date and e.air_date[:10] <= today) or e.is_watched]

    async def set_episodes_watched(self, show_id: int, keys: Iterable[Tuple[int, int]], watched: bool) -> Optional[Show]:
        """Marks specific episodes watched/unwatched locally and queues the exact change for the library."""
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        keys = sorted(set((int(s), int(n)) for s, n in keys))
        if not keys:
            return show
        now = _iso(_utcnow())
        await self.repository.set_episodes_watched(show_id, keys, watched, watched_at=now)
        if show.episodes_synced_at:
            updated = await self.repository.recompute_progress_from_episodes(show_id)
        else:
            delta = len(keys) if watched else -len(keys)
            new_count = max(0, show.watched_episodes + delta)
            if show.aired_episodes:
                new_count = min(new_count, show.aired_episodes)
            await self.repository.update_show_fields(show_id, {"watched_episodes": new_count})
            updated = await self.repository.get_show_by_id(show_id)
        fields: Dict[str, Any] = {}
        if watched:
            fields["last_watched_at"] = now
            last = max(keys)
            fields["last_watched"] = format_episode_code(*last)
            if updated.status in (ShowStatus.PLANTOWATCH, ShowStatus.HOLD, ShowStatus.DROPPED):
                fields["status"] = ShowStatus.WATCHING.value  # trackers do the same on a watch event
            if not show.episodes_synced_at:
                fields["next_to_watch"] = format_episode_code(last[0], last[1] + 1)
        if updated.is_ended and updated.aired_episodes and updated.watched_episodes >= updated.aired_episodes and watched:
            fields["status"] = ShowStatus.COMPLETED.value
        if fields:
            await self.repository.update_show_fields(show_id, fields)
        await self._queue(sync_ops.episodes_op(show, keys, watched, now), show)
        return await self.repository.get_show_by_id(show_id)

    async def mark_next_episode(self, show_id: int) -> Optional[Show]:
        """'+1': marks the next unwatched aired episode (exact season/episode) as watched."""
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        if show.media_type == MediaType.MOVIE:
            return await self.mark_movie_watched(show_id, True)
        if await self.ensure_episode_list(show):
            show = await self.repository.get_show_by_id(show_id)
            target = parse_episode_code(show.next_to_watch)
            aired = await self._ordered_aired(show_id)
            if target is None or not any((e.season, e.number) == target and not e.is_watched for e in aired):
                unwatched = [e for e in aired if not e.is_watched]
                target = (unwatched[0].season, unwatched[0].number) if unwatched else None
            if target is None:
                return show  # caught up
            return await self.set_episodes_watched(show_id, [target], True)
        target = parse_episode_code(show.next_to_watch)
        if target and await self.account_key(show):  # an account knows it (also while local): queue the episode
            return await self.set_episodes_watched(show_id, [target], True)
        # Local-only title (no account id / episode list): count only
        return await self.repository.increment_show_episode(show_id)

    # Back-compat name
    async def mark_episode_watched(self, show_id: int, season: int = 1, episode: int = 1) -> Optional[Show]:
        return await self.mark_next_episode(show_id)

    async def unmark_last_episode(self, show_id: int) -> Optional[Show]:
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        if show.media_type == MediaType.MOVIE:
            return await self.mark_movie_watched(show_id, False)
        if await self.ensure_episode_list(show):
            watched = [e for e in await self.repository.get_episodes(show_id) if e.is_watched and e.season > 0]
            if not watched:
                return show
            last = max(watched, key=lambda e: (e.season, e.number))
            return await self.set_episodes_watched(show_id, [(last.season, last.number)], False)
        return await self.repository.update_show_progress(show_id, max(0, show.watched_episodes - 1))

    async def set_watched_count(self, show_id: int, count: int) -> Optional[Show]:
        """Sets progress to 'first N aired episodes watched', pushing exactly the episodes that changed."""
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        if show.media_type == MediaType.MOVIE:
            return await self.mark_movie_watched(show_id, count > 0)
        count = max(0, count)
        if await self.ensure_episode_list(show):
            aired = await self._ordered_aired(show_id)
            count = min(count, len(aired))
            target = {(e.season, e.number) for e in aired[:count]}
            current = {(e.season, e.number) for e in aired if e.is_watched}
            to_add, to_remove = target - current, current - target
            if to_remove:
                await self.set_episodes_watched(show_id, to_remove, False)
            if to_add:
                await self.set_episodes_watched(show_id, to_add, True)
            return await self.repository.get_show_by_id(show_id)
        cap = show.aired_episodes or show.total_episodes
        if cap:
            count = min(count, cap)
        return await self.repository.update_show_progress(show_id, count)

    async def season_boundaries(self, show: Show) -> List[int]:
        """Cumulative aired-episode counts at the end of each season."""
        if show.episodes_synced_at:
            aired = await self._ordered_aired(show.id)
            bounds, total, cur = [], 0, None
            for e in aired:
                if cur is not None and e.season != cur:
                    bounds.append(total)
                cur = e.season
                total += 1
            if total:
                bounds.append(total)
            if bounds:
                return bounds
        size = await self.repository.get_show_season_size(show)
        cap = show.aired_episodes or show.total_episodes or (show.watched_episodes + size * 3)
        bounds = list(range(size, cap + 1, size))
        if not bounds or bounds[-1] != cap:
            bounds.append(cap)
        return bounds

    async def step_season(self, show_id: int, delta: int) -> Optional[Show]:
        show = await self.repository.get_show_by_id(show_id)
        if not show or delta == 0:
            return show
        if show.media_type != MediaType.MOVIE:
            await self.ensure_episode_list(show)
            show = await self.repository.get_show_by_id(show_id)
        bounds = await self.season_boundaries(show)
        w = show.watched_episodes
        if delta > 0:
            ahead = [b for b in bounds if b > w]
            target = ahead[min(delta, len(ahead)) - 1] if ahead else w
        else:
            behind = [b for b in bounds if b < w]
            steps = -delta
            target = behind[-steps] if len(behind) >= steps else 0
        return await self.set_watched_count(show_id, target)

    async def set_season_watched(self, show_id: int, season: int, watched: bool) -> Optional[Show]:
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        await self.ensure_episode_list(show)
        keys = [(e.season, e.number) for e in await self._ordered_aired(show_id) if e.season == season]
        return await self.set_episodes_watched(show_id, keys, watched)

    async def mark_movie_watched(self, show_id: int, watched: bool) -> Optional[Show]:
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        now = _iso(_utcnow())
        status = ShowStatus.COMPLETED if watched else ShowStatus.PLANTOWATCH
        await self.repository.update_show_fields(show_id, {
            "watched_episodes": 1 if watched else 0, "status": status.value,
            **({"last_watched_at": now} if watched else {}),
        })
        await self._queue(sync_ops.movie_op(show, watched, now), show)
        return await self.repository.get_show_by_id(show_id)

    async def update_show_status(self, show_id: int, status: ShowStatus) -> Optional[Show]:
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        if show.media_type == MediaType.MOVIE and status in (ShowStatus.WATCHING, ShowStatus.HOLD):
            status = ShowStatus.PLANTOWATCH  # a movie is never 'watching' or 'on hold'
        updated = await self.repository.update_show_metadata(show_id, status=status)
        if show.media_type == MediaType.MOVIE:
            await self.repository.update_show_fields(show_id, {"watched_episodes": 1 if status == ShowStatus.COMPLETED else 0})
        await self.queue_list_move(updated, status)
        return await self.repository.get_show_by_id(show_id)

    async def update_show_rating(self, show_id: int, rating: Optional[float]) -> Optional[Show]:
        show = await self.repository.get_show_by_id(show_id)
        if not show:
            return None
        if rating is None:
            updated = await self.repository.update_show_metadata(show_id, clear_user_rating=True)
            await self._queue(sync_ops.rating_op(show, None), show)
        else:
            rating = max(1, min(10, int(round(rating))))
            updated = await self.repository.update_show_metadata(show_id, user_rating=float(rating))
            await self._queue(sync_ops.rating_op(show, rating), show)
        return updated
