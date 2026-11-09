"""Derived show views (returning, stale, up next, schedule) for the repository."""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

from penguwatch import clock
from penguwatch.models import Show, ShowStatus, MediaType
from penguwatch.db.episodes import format_episode_code
from penguwatch.db.services import EFFECTIVELY_ACTIVE_SQL
from penguwatch.db.shows import household_tx, title_ids_tx


def _air_window(days_ahead: int) -> tuple:
    """ISO date bounds for schedule queries: household today -2 days up to and including +days_ahead, as
    `air_date >= first AND air_date < end`. Dates are stored as YYYY-MM-DD, some with a time after it; comparing the
    column itself (not substr/date of it) lets the air_date index serve the range, and "<" the day after keeps
    "2026-11-10T20:00:00Z" inside a window that ends on the 10th."""
    today = clock.today()
    return (today - timedelta(days=2)).isoformat(), (today + timedelta(days=int(days_ahead) + 1)).isoformat()


class ViewsMixin:
    async def get_returning_shows(self, completed: Optional[List[Show]] = None) -> List[Dict[str, Any]]:
        """Completed shows that have episodes beyond what was watched: a new season announced or already out.

        `completed` lets a caller that already loaded the completed shows (default sort order) pass them in.
        """
        today = clock.today().isoformat()
        out: List[Dict[str, Any]] = []
        if completed is None:
            completed = await self.get_shows(status=ShowStatus.COMPLETED)
        facts = await self._returning_facts(today)
        for show in completed:
            if show.media_type == MediaType.MOVIE:
                continue
            f = facts.get(show.id)
            # last: the highest watched (season, number); without any watched episode, the one the progress counter
            # points at (when the episode list was synced); None when neither is known
            if f and f["last_season"] is not None:
                last = (f["last_season"], f["last_number"])
            elif show.episodes_synced_at and show.watched_episodes:
                last = (f["fallback_season"], f["fallback_number"]) if f and f["fallback_season"] is not None else (0, 0)
            else:
                last = None
            if last is None or not f or not f["new_count"]:
                if last is None and show.next_episode_air_date and show.next_episode_air_date[:10] >= today:
                    # No episode list yet, but the calendar knows a next air date
                    out.append({
                        "show": show, "state": "upcoming", "new_count": None,
                        "first_code": format_episode_code(show.next_episode_season, show.next_episode_number),
                        "first_air_date": show.next_episode_air_date[:10], "new_season": show.next_episode_season,
                    })
                continue
            out.append({
                "show": show,
                "state": "available" if f["aired_count"] else "upcoming",
                "new_count": f["new_count"],
                "aired_count": f["aired_count"],
                "first_code": format_episode_code(f["first_season"], f["first_number"]),
                "first_air_date": f["first_air_date"][:10],
                "new_season": f["first_season"] if last is None or f["first_season"] > last[0] else None,
            })
        out.extend(await self._sequel_hints(completed))
        out.sort(key=lambda r: (0 if r["state"] == "available" else 1, r["first_air_date"] or "9999"))
        return out

    async def _returning_facts(self, today: str) -> Dict[int, Dict[str, Any]]:
        """Per completed show, computed in SQL (no Episode model per episode): the highest watched (season, number),
        the one the progress counter points at (used when nothing is marked watched), and the unwatched episodes with
        an air date after that point: their count, how many have aired, and the first one."""
        db = await self.read_connection()
        # Per show the index on (show_id, season, number) is walked, never the whole episodes table: the last watched
        # episode by a descending index lookup, the progress-counter fallback (rare: nothing marked watched) by ranking
        # only those shows' episodes, then one pass over the episodes from that point on.
        cursor = await db.execute(
            """
            WITH w AS (
                SELECT s.id, s.watched_episodes, COALESCE(s.episodes_synced_at, '') != '' AS synced,
                       (SELECT e.season FROM episodes e WHERE e.show_id = s.id AND e.season > 0 AND COALESCE(e.is_watched, 0) != 0
                        ORDER BY e.season DESC, e.number DESC LIMIT 1) AS ls,
                       (SELECT e.number FROM episodes e WHERE e.show_id = s.id AND e.season > 0 AND COALESCE(e.is_watched, 0) != 0
                        ORDER BY e.season DESC, e.number DESC LIMIT 1) AS ln
                FROM shows s WHERE s.status = 'completed'
            ),
            ranked AS (
                SELECT e.show_id, e.season, e.number,
                       ROW_NUMBER() OVER (PARTITION BY e.show_id ORDER BY e.season, e.number) AS rn,
                       COUNT(*) OVER (PARTITION BY e.show_id) AS cnt
                FROM episodes e WHERE e.season > 0
                  AND e.show_id IN (SELECT id FROM w WHERE ls IS NULL AND synced AND watched_episodes > 0)
            ),
            fb AS (
                SELECT r.show_id, r.season AS fs, r.number AS fn FROM ranked r JOIN w ON w.id = r.show_id
                WHERE r.rn = MIN(w.watched_episodes, r.cnt)
            ),
            p AS (
                SELECT w.id, w.ls, w.ln, fb.fs, fb.fn, COALESCE(w.ls, fb.fs) AS ps, COALESCE(w.ln, fb.fn) AS pn
                FROM w LEFT JOIN fb ON fb.show_id = w.id
            ),
            agg AS (
                SELECT p.id AS show_id, COUNT(*) AS new_count, SUM(substr(e.air_date, 1, 10) <= ?) AS aired_count,
                       MIN(e.season) AS first_season
                FROM p JOIN episodes e ON e.show_id = p.id AND e.season >= p.ps
                WHERE e.season > 0 AND e.air_date IS NOT NULL AND e.air_date != '' AND COALESCE(e.is_watched, 0) = 0
                  AND (e.season > p.ps OR e.number > p.pn)
                GROUP BY p.id
            )
            SELECT p.id AS show_id, p.ls AS last_season, p.ln AS last_number, p.fs AS fallback_season,
                   p.fn AS fallback_number, COALESCE(agg.new_count, 0) AS new_count,
                   COALESCE(agg.aired_count, 0) AS aired_count, agg.first_season,
                   f.number AS first_number, f.air_date AS first_air_date
            FROM p
            LEFT JOIN agg ON agg.show_id = p.id
            LEFT JOIN episodes f ON f.id = (
                SELECT e.id FROM episodes e
                WHERE e.show_id = p.id AND e.season = agg.first_season AND e.air_date IS NOT NULL AND e.air_date != ''
                  AND COALESCE(e.is_watched, 0) = 0 AND (agg.first_season > p.ps OR e.number > p.pn)
                ORDER BY e.number LIMIT 1)
            """,
            (today,),
        )
        return {r["show_id"]: dict(r) for r in await cursor.fetchall()}

    async def _sequel_hints(self, completed: List[Show]) -> List[Dict[str, Any]]:
        """Anime: a catalogue may list new seasons as separate titles, linked to the last one as "sequel" relations."""
        out: List[Dict[str, Any]] = []
        library: Dict[str, Dict[Any, int]] = {}  # provider -> {id: show id}, read when first needed
        seen_sequels: set = set()
        this_year = clock.today().year
        for show in (s for s in completed if s.media_type == MediaType.ANIME):
            for sq in show.sequels or []:
                ids = sq.get("ids") or {}
                if not ids:
                    continue
                provider, sid = next(iter(ids.items()))  # the catalogue's own id comes first
                if provider not in library:
                    library[provider] = await self.get_external_ids(provider)
                if sid in library[provider] or (provider, sid) in seen_sequels:
                    continue
                seen_sequels.add((provider, sid))
                year = sq.get("year")
                if isinstance(year, str) and year.strip().isdigit():
                    year = int(year)
                year = year if isinstance(year, int) and not isinstance(year, bool) else None  # catalogue data: trust nothing
                out.append({
                    "show": show, "kind": "sequel",
                    "state": "upcoming" if (not year or year > this_year) else "available",
                    "new_count": None, "first_code": f"sequel:{sid}", "first_air_date": f"{year}-01-01" if year else None,
                    "new_season": None, "sequel": sq,
                })
        return out

    async def get_stale_watching(self, months: int = 6, watching: Optional[List[Show]] = None) -> List[Dict[str, Any]]:
        """Shows marked 'watching' that nobody has touched for `months` (or never started)."""
        cutoff = clock.utc_now() - timedelta(days=30 * months)
        today = clock.today().isoformat()
        out: List[Dict[str, Any]] = []
        if watching is None:
            watching = await self.get_shows(status=ShowStatus.WATCHING)
        for show in watching:
            if show.media_type == MediaType.MOVIE:
                continue
            last = None
            if show.last_watched_at:
                try:
                    last = datetime.fromisoformat(show.last_watched_at.replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                except ValueError:
                    last = None
            if last is None and show.watched_episodes > 0:
                continue  # unknown activity date; don't guess
            if last is not None and last > cutoff:
                continue
            upcoming = bool(show.next_episode_air_date and show.next_episode_air_date[:10] >= today)
            if last is None:
                reason, suggested = "Marked watching, but no episodes watched yet", ShowStatus.PLANTOWATCH
            elif show.episodes_left == 0 and upcoming:
                reason, suggested = f"Caught up; next episode {show.next_episode_air_date[:10]}", ShowStatus.WATCHING
            elif show.episodes_left == 0:
                # Nothing left and nothing scheduled: done for now. If a new season appears later,
                # the "finished shows with something new" check offers to bring it back.
                reason, suggested = "Caught up, nothing new announced", ShowStatus.COMPLETED
            else:
                reason = f"{show.episodes_left} episode{'s' if show.episodes_left != 1 else ''} left"
                suggested = ShowStatus.HOLD
            out.append({
                "show": show,
                "last_watched_at": last.isoformat() if last else None,
                "days_idle": (clock.utc_now() - last).days if last else None,
                "reason": reason,
                "suggested_status": suggested.value,
                "caught_up_waiting": show.episodes_left == 0 and upcoming,
            })
        out.sort(key=lambda r: r["last_watched_at"] or "")
        return out

    async def get_up_next(self, only_active_services: bool = False, limit: int = 60) -> List[Dict[str, Any]]:
        """Shows in progress with their next unwatched aired episode (like Trakt's 'Up Next')."""
        db = await self.read_connection()
        query = f"""
            SELECT s.*, svc.name AS service_name, {EFFECTIVELY_ACTIVE_SQL} AS service_active
            FROM shows s
            LEFT JOIN services svc ON s.service_id = svc.id
            WHERE s.status = 'watching' AND COALESCE(s.media_type, 'show') != 'movie'
        """
        if only_active_services:
            query += " AND " + EFFECTIVELY_ACTIVE_SQL
        query += " ORDER BY s.last_watched_at DESC NULLS LAST, s.updated_at DESC LIMIT ?"
        cursor = await db.execute(query, (limit,))
        rows = await cursor.fetchall()
        all_ids = await title_ids_tx(db, [r["id"] for r in rows])
        people = await household_tx(db, [r["id"] for r in rows])
        results = []
        for r in rows:
            show = self._row_to_show(r, all_ids.get(r["id"]), household=people.get(r["id"]))
            entry: Dict[str, Any] = show.model_dump()
            entry["service_name"] = r["service_name"]
            entry["service_active"] = bool(r["service_active"]) if r["service_active"] is not None else False
            nxt = None
            if show.next_to_watch:
                code = show.next_to_watch.upper()
                try:
                    s_part, e_part = code.lstrip("S").split("E", 1)
                    season, number = int(s_part), int(e_part)
                    cur = await db.execute(
                        "SELECT title, air_date FROM episodes WHERE show_id = ? AND season = ? AND number = ?",
                        (show.id, season, number),
                    )
                    er = await cur.fetchone()
                    nxt = {
                        "season": season,
                        "number": number,
                        "code": format_episode_code(season, number),
                        "title": er["title"] if er else None,
                        "air_date": er["air_date"] if er else None,
                    }
                except (ValueError, IndexError):
                    nxt = {"code": show.next_to_watch}
            entry["up_next"] = nxt
            results.append(entry)
        # Shows with something to watch first, then shows waiting on new episodes
        results.sort(key=lambda e: 0 if e["episodes_left"] > 0 else 1)
        return results

    async def get_upcoming_schedule(self, days_ahead: int = 45) -> List[Dict[str, Any]]:
        """Returns upcoming episodes for tracked shows within days_ahead."""
        db = await self.read_connection()
        cursor = await db.execute(
            f"""
            SELECT s.id as id, s.title as title, s.en_title as en_title, s.poster as poster, s.network as network,
                   s.service_id as service_id, svc.name as service_name,
                   {EFFECTIVELY_ACTIVE_SQL} as is_service_active,
                   e.air_date as next_episode_air_date,
                   e.season as next_episode_season,
                   e.number as next_episode_number,
                   e.title as next_episode_title,
                   s.priority as priority
            FROM episodes e
            JOIN shows s ON e.show_id = s.id
            LEFT JOIN services svc ON s.service_id = svc.id
            WHERE e.air_date IS NOT NULL
              AND e.season > 0
              AND e.air_date >= ?
              AND e.air_date < ?
              AND s.status NOT IN ('completed', 'dropped')
            ORDER BY e.air_date ASC
            """,
            _air_window(days_ahead),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        seen_show_dates = {
            (r["id"], (r["next_episode_air_date"] or "")[:10], r.get("next_episode_season"), r.get("next_episode_number"))
            for r in rows
        }

        cursor_shows = await db.execute(
            f"""
            SELECT s.id, s.title, s.en_title, s.poster, s.network, s.service_id, svc.name as service_name,
                   {EFFECTIVELY_ACTIVE_SQL} as is_service_active,
                   s.next_episode_air_date, s.next_episode_season, s.next_episode_number,
                   s.next_episode_title, s.priority
            FROM shows s
            LEFT JOIN services svc ON s.service_id = svc.id
            WHERE s.next_episode_air_date IS NOT NULL
              AND s.next_episode_air_date >= ?
              AND s.next_episode_air_date < ?
              AND s.status NOT IN ('completed', 'dropped')
            ORDER BY s.next_episode_air_date ASC
            """,
            _air_window(days_ahead),
        )
        for fr in await cursor_shows.fetchall():
            f_dict = dict(fr)
            key = (
                f_dict["id"],
                (f_dict["next_episode_air_date"] or "")[:10],
                f_dict.get("next_episode_season"),
                f_dict.get("next_episode_number"),
            )
            if key not in seen_show_dates:
                rows.append(f_dict)
                seen_show_dates.add(key)

        rows.sort(key=lambda r: (r["next_episode_air_date"] or ""))
        people = await household_tx(db, {r["id"] for r in rows})
        for r in rows:
            r["household"] = self._household_of(people.get(r["id"]))
            if r.get("en_title") is None:  # absent unless known, like Show.en_title
                r.pop("en_title", None)
        return rows
