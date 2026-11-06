"""Episode queries for the repository."""

import json
import re
from typing import List, Optional, Dict, Any, Iterable, Tuple

from penguwatch import clock
from penguwatch.models import Show, Episode, MediaType, ShowStatus
from penguwatch.db._common import _utcnow
from penguwatch.db.shows import title_ref_tx
from penguwatch.drivers.base import normal_ids


def _ids_json(ids: Optional[Dict[str, Any]]) -> Optional[str]:
    """An episode's provider ids as stored (None when there are none, so a stored set is kept)."""
    clean = normal_ids(ids)
    return json.dumps(clean, sort_keys=True) if clean else None


def _ids_from_json(raw: Optional[str]) -> Dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return normal_ids(value) if isinstance(value, dict) else {}


def format_episode_code(season: Optional[int], number: Optional[int]) -> Optional[str]:
    if season is None or number is None:
        return None
    return f"S{int(season):02d}E{int(number):02d}"


def parse_episode_code(code: Optional[str]) -> Optional[Tuple[int, int]]:
    """(season, number) from "S01E02"; "E12" (absolute numbering, common for anime) is season 1."""
    if not code:
        return None
    m = re.match(r"^\s*S(\d+)\s*E(\d+)", str(code), re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^\s*E(\d+)", str(code), re.IGNORECASE)
    if m:
        return 1, int(m.group(1))
    return None


class EpisodesMixin:

    async def upsert_episode_list(self, show_id: int, episodes: List[Dict[str, Any]]) -> None:
        """Stores a show's full episode list (titles + air dates), keeping existing watched flags."""
        if not episodes:
            return
        async with self.write() as db:
            await db.executemany(
                """
                INSERT INTO episodes (show_id, ids_json, season, number, title, air_date, is_watched)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(show_id, season, number) DO UPDATE SET
                    title = COALESCE(excluded.title, episodes.title),
                    air_date = COALESCE(excluded.air_date, episodes.air_date),
                    ids_json = CASE WHEN excluded.ids_json IS NULL THEN episodes.ids_json
                                    ELSE json_patch(COALESCE(episodes.ids_json, '{}'), excluded.ids_json) END
                """,
                [
                    (show_id, _ids_json(ep.get("ids")), ep["season"], ep["number"], ep.get("title"), ep.get("air_date"))
                    for ep in episodes
                ],
            )

    # Kept for compatibility: the upcoming-schedule sync uses the same upsert.
    async def upsert_upcoming_episodes(self, episodes: List[Dict[str, Any]]):
        by_show: Dict[int, List[Dict[str, Any]]] = {}
        for ep in episodes:
            by_show.setdefault(ep["show_id"], []).append(ep)
        for show_id, eps in by_show.items():
            await self.upsert_episode_list(show_id, eps)

    @staticmethod
    def _row_to_episode(r) -> Episode:
        return Episode(
            id=r["id"],
            show_id=r["show_id"],
            ids=_ids_from_json(r["ids_json"]),
            season=r["season"],
            number=r["number"],
            title=r["title"],
            air_date=r["air_date"],
            is_watched=bool(r["is_watched"]),
            watched_at=r["watched_at"],
        )

    async def get_episodes(self, show_id: int) -> List[Episode]:
        db = await self.read_connection()
        return await self._episodes_tx(db, show_id)

    async def _episodes_tx(self, db, show_id: int) -> List[Episode]:
        """get_episodes on any connection (the write transaction's, to see what it has written)."""
        cursor = await db.execute(
            "SELECT * FROM episodes WHERE show_id = ? ORDER BY season ASC, number ASC", (show_id,)
        )
        return [self._row_to_episode(r) for r in await cursor.fetchall()]

    async def get_episodes_by_show(self, status: ShowStatus) -> Dict[int, List[Episode]]:
        """Every episode of the shows with this status in one query (instead of one query per show)."""
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT e.* FROM episodes e JOIN shows s ON e.show_id = s.id WHERE s.status = ? "
            "ORDER BY e.show_id ASC, e.season ASC, e.number ASC",
            (status.value,),
        )
        grouped: Dict[int, List[Episode]] = {}
        for r in await cursor.fetchall():
            grouped.setdefault(r["show_id"], []).append(self._row_to_episode(r))
        return grouped

    async def set_episodes_watched(
        self,
        show_id: int,
        keys: Iterable[Tuple[int, int]],
        watched: bool,
        watched_at: Optional[str] = None,
    ) -> int:
        """Marks (season, number) pairs watched/unwatched, creating placeholder rows if needed."""
        keys = list(keys)
        if not keys:
            return 0
        ts = (watched_at or _utcnow()) if watched else None
        async with self.write() as db:
            newly = []
            if watched and self._event_listeners:
                done = {(r["season"], r["number"]) for r in await (await db.execute(
                    "SELECT season, number FROM episodes WHERE show_id = ? AND is_watched = 1", (show_id,))).fetchall()}
                newly = [k for k in keys if k not in done]
            await db.executemany(
                """
                INSERT INTO episodes (show_id, season, number, is_watched, watched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(show_id, season, number) DO UPDATE SET
                    is_watched = excluded.is_watched,
                    watched_at = CASE WHEN excluded.is_watched = 1 THEN COALESCE(episodes.watched_at, excluded.watched_at) ELSE NULL END
                """,
                [(show_id, s, n, 1 if watched else 0, ts) for s, n in keys],
            )
            if newly:
                ref = await title_ref_tx(db, show_id)
                if ref is not None:
                    self.queue_event("episode.watched", {
                        "title": ref, "watched_at": ts,
                        "episodes": [{"season": s, "number": n, "code": format_episode_code(s, n)} for s, n in sorted(newly)[:100]],
                        "count": len(newly)})
        return len(keys)

    async def replace_watched_episodes(
        self, show_id: int, watched: List[Tuple[int, int, Optional[str]]]
    ) -> None:
        """Makes the local watched set exactly match the library's list for this show."""
        async with self.write() as db:
            await self._replace_watched_episodes_tx(db, show_id, watched)

    async def _replace_watched_episodes_tx(
        self, db, show_id: int, watched: List[Tuple[int, int, Optional[str]]]
    ) -> None:
        await db.execute("UPDATE episodes SET is_watched = 0, watched_at = NULL WHERE show_id = ?", (show_id,))
        if watched:
            await db.executemany(
                """
                INSERT INTO episodes (show_id, season, number, is_watched, watched_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(show_id, season, number) DO UPDATE SET is_watched = 1, watched_at = excluded.watched_at
                """,
                [(show_id, s, n, at) for s, n, at in watched],
            )

    async def recompute_progress_from_episodes(self, show_id: int) -> Optional[Show]:
        """Derives counts, 'up next' and next air date from the stored episode list.

        Only applies once the full episode list has been fetched (episodes_synced_at set);
        until then the counters the library reported are kept.
        """
        async with self.write() as db:
            await self._recompute_progress_tx(db, show_id)
        return await self.get_show_by_id(show_id)

    async def _recompute_progress_tx(self, db, show_id: int) -> None:
        """recompute_progress_from_episodes inside an open write transaction."""
        show = await self._show_by_id_tx(db, show_id)
        if not show or not show.episodes_synced_at or show.media_type == MediaType.MOVIE:
            return
        episodes = [e for e in await self._episodes_tx(db, show_id) if e.season > 0]
        if not episodes:
            return
        today = clock.today().isoformat()

        def has_aired(e: Episode) -> bool:
            return bool(e.air_date) and e.air_date[:10] <= today

        aired = [e for e in episodes if has_aired(e) or e.is_watched]
        watched = [e for e in episodes if e.is_watched]
        unwatched_aired = [e for e in aired if not e.is_watched]
        future = sorted((e for e in episodes if e.air_date and e.air_date[:10] > today), key=lambda e: e.air_date)
        last = max(watched, key=lambda e: (e.season, e.number)) if watched else None
        # Up next = first unwatched aired episode after the last watched one (falls back to earliest gap)
        nxt = None
        if last:
            after = [e for e in unwatched_aired if (e.season, e.number) > (last.season, last.number)]
            nxt = after[0] if after else None
        if nxt is None and unwatched_aired:
            nxt = unwatched_aired[0]

        fields: Dict[str, Any] = {
            "watched_episodes": len(watched),
            "aired_episodes": len(aired),
            "total_episodes": max(len(episodes), show.total_episodes if show.is_ended else 0),
            "last_watched": format_episode_code(last.season, last.number) if last else None,
            "next_to_watch": format_episode_code(nxt.season, nxt.number) if nxt else None,
            "next_episode_air_date": future[0].air_date[:10] if future else None,
            "next_episode_season": future[0].season if future else None,
            "next_episode_number": future[0].number if future else None,
            "next_episode_title": future[0].title if future else None,
        }
        await self._update_show_fields_tx(db, show_id, fields)
