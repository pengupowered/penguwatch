"""History tab queries: the raw rows behind history.build_history()."""

import copy
from typing import Any, Dict, List

from penguwatch import clock
from penguwatch.history import BULK_MIN, Window, build_history, zone_name



class HistoryMixin:
    async def get_history(self, days: int, include_bulk: bool = False, bulk_min: int = BULK_MIN) -> Dict[str, Any]:
        """The History payload for the last `days` local days against the `days` before.

        Reused until the next write (per period, bulk setting, bulk threshold, zone and local date, since the window
        moves with the date); each caller gets its own copy. `bulk_min` is the setting history.bulk_min and is part of
        the cache key, so changing it never serves an answer made with the old one."""
        window = Window(days=days, today=clock.today(), tz=clock.tz())
        suffix = f"{zone_name(window.tz)}:{window.today}"
        for key in [k for k in self._derived if k.startswith("history:") and not k.endswith(suffix)]:
            del self._derived[key]  # another day's (or zone's) entries
        data = await self.derived(f"history:{days}:{int(include_bulk)}:{bulk_min}:{suffix}",
                                  lambda: self._build_history(window, include_bulk, bulk_min))
        return copy.deepcopy(data)

    async def _build_history(self, window: Window, include_bulk: bool, bulk_min: int) -> Dict[str, Any]:
        data = build_history(await self._history_rows(window), window, include_bulk, bulk_min)
        posters = await self._posters([s["id"] for s in data["top_shows"]])
        for s in data["top_shows"]:
            s["poster"] = posters.get(s["id"])
        return data

    async def _history_rows(self, window: Window) -> Dict[str, Any]:
        db = await self.read_connection()
        lo, hi = window.query_bounds()
        cur = await db.execute(
            """SELECT e.show_id, e.watched_at, COALESCE(s.runtime_minutes, 45) AS minutes
               FROM episodes e JOIN shows s ON s.id = e.show_id
               WHERE e.is_watched = 1 AND e.watched_at >= ? AND e.watched_at < ?""",
            (lo, hi),
        )
        episodes = [(r[0], r[1], r[2]) for r in await cur.fetchall()]
        # a movie has no episode rows: one watch, at its last-watched stamp (the rule Insights uses)
        cur = await db.execute(
            """SELECT id, last_watched_at, COALESCE(runtime_minutes, 110) AS minutes
               FROM shows WHERE media_type = 'movie' AND status = 'completed'
                 AND last_watched_at >= ? AND last_watched_at < ?
                 AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.show_id = shows.id AND e.is_watched = 1)""",
            (lo, hi),
        )
        movies = [(r[0], r[1], r[2]) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT last_watched_at FROM shows WHERE status = 'completed' AND last_watched_at >= ? AND last_watched_at < ?",
            (lo, hi),
        )
        finished = [r[0] for r in await cur.fetchall()]

        in_window = ("SELECT e.show_id FROM episodes e WHERE e.is_watched = 1 AND e.watched_at >= :lo AND e.watched_at < :hi")
        cur = await db.execute(
            f"""SELECT id, title, service_id, genres_json FROM shows WHERE id IN ({in_window})
                UNION ALL
                SELECT id, title, service_id, genres_json FROM shows
                WHERE media_type = 'movie' AND status = 'completed' AND last_watched_at >= :lo AND last_watched_at < :hi
                  AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.show_id = shows.id AND e.is_watched = 1)""",
            {"lo": lo, "hi": hi})
        shows = {r[0]: {"title": r[1], "service_id": r[2], "genres_json": r[3]} for r in await cur.fetchall()}
        # Titles with a watched episode before the two periods were not started in them; the others started at their
        # earliest stamp among the rows above (one probe per title on the episodes(show_id) index; the unary + keeps
        # SQLite from walking the (is_watched, watched_at) index for every title instead)
        cur = await db.execute(
            f"""SELECT s.id FROM shows s WHERE s.id IN ({in_window}) AND EXISTS (
                    SELECT 1 FROM episodes e WHERE e.show_id = s.id AND +e.is_watched = 1 AND +e.watched_at < :lo)""",
            {"lo": lo, "hi": hi})
        older = {r[0] for r in await cur.fetchall()}

        cur = await db.execute("SELECT service_id, monthly_cost, effective_from FROM service_price_history "
                               "ORDER BY service_id, effective_from, id")
        prices: Dict[str, List[Any]] = {}
        for r in await cur.fetchall():
            prices.setdefault(r[0], []).append((r[2], r[1]))
        services = [{"id": s.id, "name": s.name, "monthly_cost": s.monthly_cost, "is_active": s.is_active}
                    for s in await self.get_services()]
        return {"episodes": episodes, "movies": movies, "finished": finished, "shows": shows,
                "older": older, "prices": prices, "services": services}

    async def _posters(self, ids: List[int]) -> Dict[int, str]:
        if not ids:
            return {}
        db = await self.read_connection()
        cur = await db.execute(f"SELECT id, poster FROM shows WHERE id IN ({','.join('?' * len(ids))})", ids)
        return {r[0]: r[1] for r in await cur.fetchall() if r[1]}
