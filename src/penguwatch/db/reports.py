"""Insights, dashboard and system-snapshot queries for the repository."""

import copy
import os
from typing import Optional, Dict, Any, List
from datetime import datetime, date, timedelta
from penguwatch import clock
from penguwatch.insights import build_insights
from penguwatch.optimizer import bundles


class ReportsMixin:
    async def get_insights(self, months: int = 12) -> Dict[str, Any]:
        """Viewing history aggregates for the Insights tab (all from watched episodes + completed movies)."""
        months = max(1, min(int(months), 36))
        return build_insights(await self.get_insight_rows(months), months)

    async def get_insight_rows(self, months: int) -> Dict[str, Any]:
        """Raw aggregates behind get_insights(); insights.build_insights() shapes them.

        Reused until the next write (per month count and calendar day, as the windows move with the date); each
        caller gets its own copy."""
        months = max(1, min(int(months), 36))
        today = datetime.now(clock.tz()).date()
        for key in [k for k in self._derived if k.startswith("insight_rows:") and not k.endswith(f":{today}")]:
            del self._derived[key]  # a past day's entries
        rows = await self.derived(f"insight_rows:{months}:{today}", lambda: self._insight_rows(months, today))
        return copy.deepcopy(rows)

    async def _insight_rows(self, months: int, today: date) -> Dict[str, Any]:
        first = date(today.year, today.month, 1)
        for _ in range(months - 1):
            first = (first.replace(day=1) - timedelta(days=1)).replace(day=1)
        db = await self.read_connection()
        since = first.isoformat()
        cutoff90 = (today - timedelta(days=90)).isoformat()
        cur = await db.execute(
            "SELECT COUNT(DISTINCT substr(watched_at, 1, 10)) AS d FROM episodes WHERE is_watched = 1 AND watched_at >= ?",
            (cutoff90,),
        )
        active_days = (await cur.fetchone())["d"]
        cur = await db.execute("SELECT status, COUNT(*) AS n FROM shows GROUP BY status")
        statuses = {r["status"]: r["n"] for r in await cur.fetchall()}
        return {
            "first": since,
            "episode_months": await self._insight_episode_months(db, since),
            "movie_months": await self._insight_movie_months(db, since),
            "genre_rows": await self._insight_genre_rows(db, since),
            "weekday_rows": await self._insight_weekday_rows(db, since),
            "active_days": active_days,
            "statuses": statuses,
            "by_service": await self.get_service_value(months * 30),
        }

    @staticmethod
    async def _insight_episode_months(db, since: str) -> List[Dict[str, Any]]:
        cur = await db.execute(
            """SELECT substr(e.watched_at, 1, 7) AS ym, COUNT(*) AS n, SUM(COALESCE(s.runtime_minutes, 45)) / 60.0 AS hours
               FROM episodes e JOIN shows s ON s.id = e.show_id
               WHERE e.is_watched = 1 AND e.watched_at >= ? GROUP BY ym""",
            (since,),
        )
        return [dict(r) for r in await cur.fetchall()]

    @staticmethod
    async def _insight_movie_months(db, since: str) -> List[Dict[str, Any]]:
        cur = await db.execute(
            """SELECT substr(last_watched_at, 1, 7) AS ym, COUNT(*) AS n, SUM(COALESCE(runtime_minutes, 110)) / 60.0 AS hours
               FROM shows WHERE media_type = 'movie' AND status = 'completed' AND last_watched_at >= ?
                 AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.show_id = shows.id AND e.is_watched = 1)
               GROUP BY ym""",
            (since,),
        )
        return [dict(r) for r in await cur.fetchall()]

    @staticmethod
    async def _insight_genre_rows(db, since: str) -> List[Dict[str, Any]]:
        # hours per show first; the genres (a JSON list) are folded in Python
        cur = await db.execute(
            """SELECT s.id, s.genres_json, COUNT(*) AS n, SUM(COALESCE(s.runtime_minutes, 45)) / 60.0 AS hours
               FROM episodes e JOIN shows s ON s.id = e.show_id
               WHERE e.is_watched = 1 AND e.watched_at >= ? GROUP BY s.id""",
            (since,),
        )
        return [dict(r) for r in await cur.fetchall()]

    @staticmethod
    async def _insight_weekday_rows(db, since: str) -> List[Dict[str, Any]]:
        cur = await db.execute(
            """SELECT CAST(strftime('%w', substr(e.watched_at, 1, 10)) AS INTEGER) AS dow, COUNT(*) AS n
               FROM episodes e WHERE e.is_watched = 1 AND e.watched_at >= ? GROUP BY dow""",
            (since,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_service_value(self, days: int = 90) -> Dict[str, Dict[str, float]]:
        """Episodes and hours actually watched per service over the last `days` days.

        Movies have no episode rows, so they are counted once at `last_watched_at`. The dates are compared as stored
        (YYYY-MM-DD, some with a time after it) so the (is_watched, watched_at) index can serve the range."""
        cutoff = (datetime.now(clock.tz()).date() - timedelta(days=days)).isoformat()
        db = await self.read_connection()
        out: Dict[str, Dict[str, float]] = {}

        def add(service_id: Optional[str], episodes: int, hours: float) -> None:
            row = out.setdefault(service_id or "", {"episodes": 0, "hours": 0.0})
            row["episodes"] += episodes
            row["hours"] += hours

        cur = await db.execute(
            """SELECT s.service_id AS service_id, COUNT(*) AS episodes,
                      COALESCE(SUM(COALESCE(s.runtime_minutes, 45)), 0) / 60.0 AS hours
               FROM episodes e JOIN shows s ON s.id = e.show_id
               WHERE e.is_watched = 1 AND e.watched_at >= ?
               GROUP BY s.service_id""",
            (cutoff,),
        )
        for r in await cur.fetchall():
            add(r["service_id"], r["episodes"], r["hours"])
        cur = await db.execute(
            """SELECT service_id, COUNT(*) AS episodes, COALESCE(SUM(COALESCE(runtime_minutes, 110)), 0) / 60.0 AS hours
               FROM shows
               WHERE media_type = 'movie' AND status = 'completed' AND last_watched_at >= ?
                 AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.show_id = shows.id AND e.is_watched = 1)
               GROUP BY service_id""",
            (cutoff,),
        )
        for r in await cur.fetchall():
            add(r["service_id"], r["episodes"], r["hours"])
        return out

    async def get_system_snapshot(self) -> Dict[str, Any]:
        """Row counts, outbox health and file sizes for /metrics and the System tab."""
        db = await self.read_connection()
        cur = await db.execute("SELECT status, COUNT(*) AS n FROM shows GROUP BY status")
        by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
        cur = await db.execute("SELECT COUNT(*) AS n FROM episodes")
        episodes = (await cur.fetchone())["n"]
        cur = await db.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN approved = 0 THEN 1 ELSE 0 END), 0) AS awaiting,
                      COALESCE(SUM(CASE WHEN approved = 1 AND attempts > 0 THEN 1 ELSE 0 END), 0) AS failing,
                      MIN(created_at) AS oldest
               FROM sync_outbox"""
        )
        outbox = dict(await cur.fetchone())
        cur = await db.execute("SELECT COUNT(*) AS n FROM app_state")
        state_keys = (await cur.fetchone())["n"]

        def size(p: str) -> int:
            try:
                return os.path.getsize(p)
            except OSError:
                return 0

        return {
            "shows_by_status": by_status,
            "shows": sum(by_status.values()),
            "episodes": episodes,
            "outbox": outbox,
            "state_keys": state_keys,
            "db_bytes": size(self.db_path),
            "wal_bytes": size(self.db_path + "-wal"),
        }

    # --- Dashboard Summary Statistics ---

    async def get_dashboard_stats(self) -> Dict[str, Any]:
        db = await self.read_connection()
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) as total_shows,
                COALESCE(SUM(CASE WHEN status = 'watching' THEN 1 ELSE 0 END), 0) as watching_count,
                COALESCE(SUM(CASE WHEN status = 'plantowatch' THEN 1 ELSE 0 END), 0) as plantowatch_count,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) as completed_count,
                COALESCE(SUM(CASE WHEN COALESCE(media_type,'show') = 'movie' THEN 1 ELSE 0 END), 0) as movies_count,
                COALESCE(SUM(CASE WHEN household_tag = 'both' THEN 1 ELSE 0 END), 0) as both_want_count,
                COALESCE(SUM(CASE WHEN status NOT IN ('completed', 'dropped') AND aired_episodes > watched_episodes THEN (aired_episodes - watched_episodes) ELSE 0 END), 0) as total_unwatched_episodes,
                COALESCE(SUM(CASE WHEN status NOT IN ('completed', 'dropped') AND aired_episodes > watched_episodes THEN (aired_episodes - watched_episodes) * runtime_minutes ELSE 0 END) / 60.0, 0.0) as total_backlog_hours,
                COALESCE(SUM(watched_episodes * runtime_minutes) / 60.0, 0.0) as total_watched_hours
            FROM shows
            """
        )
        show_stats = dict(await cursor.fetchone() or {})

        services = await self.get_services()
        active_svc = {
            "active_services_count": bundles.paying_count(services),
            "current_monthly_spend": bundles.monthly_spend(services),
        }

        cursor = await db.execute("SELECT COALESCE(SUM(monthly_cost), 0.0) as all_services_spend FROM services")
        all_svc = dict(await cursor.fetchone() or {})

        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE is_watched = 1 AND watched_at >= ?",
            ((clock.utc_now() - timedelta(days=30)).isoformat(),),
        )
        recent = dict(await cursor.fetchone() or {})

        current_spend = active_svc.get("current_monthly_spend", 0.0)
        all_spend = all_svc.get("all_services_spend", 0.0)
        monthly_savings = max(0.0, all_spend - current_spend)

        return {
            **show_stats,
            "total_watched_hours": round(show_stats.get("total_watched_hours") or 0.0, 1),
            "episodes_watched_30d": recent.get("n", 0),
            "active_services_count": active_svc.get("active_services_count", 0),
            "current_monthly_spend": round(current_spend, 2),
            "all_services_spend": round(all_spend, 2),
            "monthly_savings": round(monthly_savings, 2),
            "annual_savings": round(monthly_savings * 12.0, 2),
        }
