"""Show queries for the repository."""

import json
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple
import aiosqlite

from penguwatch.drivers.base import normal_ids
from penguwatch.events import title_ref
from penguwatch.models import Show, ShowStatus, Priority, HouseholdTag, MediaType
from penguwatch.db._common import _utcnow
from penguwatch.db.services import EFFECTIVELY_ACTIVE_SQL
from penguwatch.ratings import BUILTIN_COLUMNS
from penguwatch.db.titles import SEARCH_SOURCE_COLUMNS, refresh_search_text, search_clause
from penguwatch.textsearch import clean_alt_titles, clean_en_title

# Columns the sync engine / API may write through update_show_fields(). The key "ids" adds or replaces external ids
# (title_ids); ids not given are kept.
UPDATABLE_SHOW_COLUMNS = {
    "media_type", "title", "year", "poster", "banner",
    "overview", "genres_json", "network", "service_id", "service_manual", "status", "priority",
    "household_tag", "simkl_rating", "imdb_rating", "imdb_votes", "user_rating", "total_episodes", "watched_episodes",
    "aired_episodes", "runtime_minutes", "next_episode_air_date", "next_episode_season",
    "next_episode_number", "next_episode_title", "is_ended", "last_watched", "next_to_watch", "notes",
    "last_watched_at", "episodes_synced_at", "enriched_at", "sequels_json", "en_title", "alt_titles_json",
}


_MAX_IN_PARAMS = 500  # bound parameters per IN (...) list, well under SQLite's limit


class ShowColumn(NamedTuple):
    """One shows column written by upsert_show.

    `update_sql` is the right-hand side of the UPDATE assignment (None = never touched on update,
    e.g. household-only columns); each `?` in it is bound to the update value, so a template
    with two placeholders binds the value twice. Column names come from this table only;
    values are always bound as parameters."""

    name: str
    insert_value: Callable[[Show, str], Any]
    update_sql: Optional[str] = None
    update_value: Optional[Callable[[Show, str], Any]] = None


def _attr(name: str) -> Callable[[Show, str], Any]:
    return lambda s, now: getattr(s, name)


def _enum(name: str) -> Callable[[Show, str], Any]:
    return lambda s, now: getattr(s, name).value


def _flag(name: str) -> Callable[[Show, str], int]:
    return lambda s, now: 1 if getattr(s, name) else 0


_KEEP_IF_NULL = "COALESCE(?, {c})"
_ALWAYS = "?"
_KEEP_IF_ZERO = "CASE WHEN ? > 0 THEN ? ELSE {c} END"
_KEEP_IF_MANUAL = "CASE WHEN service_manual = 1 THEN service_id ELSE COALESCE(?, service_id) END"


def _col(name, insert_value=None, update_sql=None, update_value=None) -> ShowColumn:
    insert_value = insert_value or _attr(name)
    update_sql = update_sql.format(c=name) if update_sql else None
    return ShowColumn(name, insert_value, update_sql, update_value or insert_value)


_genres_json = lambda s, now: json.dumps(s.genres) if s.genres else "[]"  # noqa: E731

# Order is the INSERT column order. The media_type value differs between the two paths:
# insert falls back to 'show', update passes None so COALESCE keeps the stored value.
SHOW_WRITE_COLUMNS: Tuple[ShowColumn, ...] = (
    _col("media_type", lambda s, now: (s.media_type or MediaType.SHOW).value, _KEEP_IF_NULL,
         lambda s, now: s.media_type.value if s.media_type else None),
    _col("title", update_sql=_ALWAYS),
    _col("en_title", lambda s, now: clean_en_title(s.title, s.en_title), _KEEP_IF_NULL),
    _col("alt_titles_json", lambda s, now: json.dumps(s.alt_titles) if s.alt_titles else None, _KEEP_IF_NULL),
    _col("year", update_sql=_KEEP_IF_NULL),
    _col("poster", update_sql=_KEEP_IF_NULL),
    _col("banner", update_sql=_KEEP_IF_NULL),
    _col("overview", update_sql=_KEEP_IF_NULL),
    _col("genres_json", _genres_json, _ALWAYS),
    _col("network", update_sql=_KEEP_IF_NULL),
    _col("service_id", update_sql=_KEEP_IF_MANUAL),
    _col("service_manual", _flag("service_manual")),
    _col("status", _enum("status"), _ALWAYS),
    _col("priority", _enum("priority")),
    _col("household_tag", _enum("household_tag")),
    _col("simkl_rating", update_sql=_KEEP_IF_NULL),
    _col("imdb_rating", update_sql=_KEEP_IF_NULL),
    _col("imdb_votes", update_sql=_KEEP_IF_NULL),
    _col("user_rating", update_sql=_KEEP_IF_NULL),
    _col("total_episodes", update_sql=_KEEP_IF_ZERO),
    _col("watched_episodes", update_sql=_ALWAYS),
    _col("aired_episodes", update_sql=_KEEP_IF_ZERO),
    _col("runtime_minutes", update_sql=_KEEP_IF_NULL),
    _col("next_episode_air_date", update_sql=_KEEP_IF_NULL),
    _col("next_episode_season", update_sql=_KEEP_IF_NULL),
    _col("next_episode_number", update_sql=_KEEP_IF_NULL),
    _col("next_episode_title", update_sql=_KEEP_IF_NULL),
    _col("is_ended", _flag("is_ended"), _ALWAYS),
    _col("last_watched", update_sql=_KEEP_IF_NULL),
    _col("next_to_watch", update_sql=_KEEP_IF_NULL),
    _col("notes"),
    _col("last_watched_at", update_sql=_KEEP_IF_NULL),
    _col("enriched_at"),
    _col("created_at", lambda s, now: now),
    _col("updated_at", lambda s, now: now, _ALWAYS),
)

_SHOW_INSERT_SQL = (
    f"INSERT INTO shows ({', '.join(c.name for c in SHOW_WRITE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in SHOW_WRITE_COLUMNS)})"
)
_SHOW_UPDATE_SQL = (
    "UPDATE shows SET "
    + ", ".join(f"{c.name} = {c.update_sql}" for c in SHOW_WRITE_COLUMNS if c.update_sql)
    + " WHERE id = ?"
)


async def title_ids_tx(db, show_ids: Optional[Iterable[int]] = None) -> Dict[int, Dict[str, Any]]:
    """{show id: {provider: external id}} for the given titles (all titles when None), on any connection."""
    if show_ids is None:
        cursor = await db.execute("SELECT show_id, provider, external_id FROM title_ids ORDER BY show_id, provider")
        rows = await cursor.fetchall()
    else:
        ids = list(dict.fromkeys(show_ids))
        rows = []
        for start in range(0, len(ids), _MAX_IN_PARAMS):
            chunk = ids[start:start + _MAX_IN_PARAMS]
            cursor = await db.execute(
                f"SELECT show_id, provider, external_id FROM title_ids WHERE show_id IN ({', '.join('?' for _ in chunk)}) "
                "ORDER BY show_id, provider", chunk)
            rows.extend(await cursor.fetchall())
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        out.setdefault(r[0], {})[r[1]] = r[2]
    return out


async def title_ref_tx(db, show_id: int) -> Optional[Dict[str, Any]]:
    """events.title_ref for a stored title, read on `db` (a write transaction sees its own rows)."""
    row = await (await db.execute("SELECT id, title, year, media_type FROM shows WHERE id = ?", (show_id,))).fetchone()
    if row is None:
        return None
    return title_ref(row, (await title_ids_tx(db, [show_id])).get(show_id, {}))


async def write_title_ids_tx(db, show_id: int, ids: Dict[str, Any]) -> None:
    """Adds or replaces the given external ids of a title (the others stay)."""
    rows = [(show_id, provider, value) for provider, value in normal_ids(ids).items()]
    if rows:
        await db.executemany(
            "INSERT INTO title_ids (show_id, provider, external_id) VALUES (?, ?, ?) "
            "ON CONFLICT(show_id, provider) DO UPDATE SET external_id = excluded.external_id", rows)


class ShowsMixin:
    # --- Shows Operations ---

    # The providers whose id identifies a title in this library (the library driver's): a title written without its
    # local id is the stored title with the same id from one of these. Set from config at start-up.
    key_providers: Tuple[str, ...] = ()

    def _row_to_show(self, r: aiosqlite.Row, ids: Optional[Dict[str, Any]] = None, trusted: bool = False) -> Show:
        """The stored title as a Show. `trusted` skips pydantic's validation (the cached whole-library parse, where
        every value is a stored, already validated one and the enums are converted below); the ids are normalised
        first, as the model's validator would."""
        keys = set(r.keys())

        def col(name, default=None):
            return r[name] if name in keys else default

        genres = json.loads(r["genres_json"]) if r["genres_json"] else []
        media_raw = col("media_type") or "show"
        try:
            media_type = MediaType(media_raw)
        except ValueError:
            media_type = MediaType.SHOW
        build = Show.model_construct if trusted else Show
        return build(
            id=r["id"],
            ids=normal_ids(ids) if trusted else (ids or {}),
            media_type=media_type,
            title=r["title"],
            en_title=col("en_title"),
            alt_titles=clean_alt_titles(json.loads(col("alt_titles_json") or "[]")),
            year=r["year"],
            poster=r["poster"],
            banner=r["banner"],
            overview=r["overview"],
            genres=genres,
            network=r["network"],
            service_id=r["service_id"],
            service_manual=bool(col("service_manual", 0)),
            status=ShowStatus(r["status"]) if r["status"] else ShowStatus.PLANTOWATCH,
            priority=Priority(r["priority"]) if r["priority"] else Priority.MEDIUM,
            household_tag=HouseholdTag(r["household_tag"]) if r["household_tag"] else HouseholdTag.BOTH,
            simkl_rating=r["simkl_rating"],
            imdb_rating=r["imdb_rating"],
            imdb_votes=r["imdb_votes"],
            user_rating=r["user_rating"],
            total_episodes=r["total_episodes"] or 0,
            watched_episodes=r["watched_episodes"] or 0,
            aired_episodes=r["aired_episodes"] or 0,
            runtime_minutes=r["runtime_minutes"] or 45,
            next_episode_air_date=r["next_episode_air_date"],
            next_episode_season=r["next_episode_season"],
            next_episode_number=r["next_episode_number"],
            next_episode_title=r["next_episode_title"],
            is_ended=bool(r["is_ended"]),
            last_watched=col("last_watched"),
            next_to_watch=col("next_to_watch"),
            notes=r["notes"],
            last_watched_at=r["last_watched_at"],
            episodes_synced_at=col("episodes_synced_at"),
            enriched_at=col("enriched_at"),
            sequels=json.loads(col("sequels_json")) if col("sequels_json") else None,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def get_shows(
        self,
        service_id: Optional[str] = None,
        only_active_services: bool = False,
        status: Optional[ShowStatus] = None,
        priority: Optional[Priority] = None,
        household_tag: Optional[HouseholdTag] = None,
        search_query: Optional[str] = None,
        sort_by: str = "priority",  # priority, <built-in source id>, title, next_air, recent, my_rating, rating:<source id>
        media_type: Optional[MediaType] = None,
        min_ratings: Iterable[Tuple[str, float]] = (),  # (source id, lowest value): titles rated at least that
    ) -> List[Show]:
        db = await self.read_connection()
        query = "SELECT s.* FROM shows s"
        conditions = []
        params: List[Any] = []

        if only_active_services:
            query += " JOIN services svc ON s.service_id = svc.id"
            conditions.append(EFFECTIVELY_ACTIVE_SQL)
        elif service_id:
            if service_id in ("unassigned", "none"):
                conditions.append("s.service_id IS NULL")
            else:
                conditions.append("s.service_id = ?")
                params.append(service_id)

        if status:
            conditions.append("s.status = ?")
            params.append(status.value)
        if priority:
            conditions.append("s.priority = ?")
            params.append(priority.value)
        if household_tag:
            conditions.append("s.household_tag = ?")
            params.append(household_tag.value)
        if media_type:
            conditions.append("COALESCE(s.media_type, 'show') = ?")
            params.append(media_type.value)
        if search_query:
            clause, clause_params = search_clause(search_query)
            conditions.append(clause)
            params.extend(clause_params)

        min_ratings = list(min_ratings)
        rating_sort = sort_by[len("rating:"):] if sort_by.startswith("rating:") else None
        await self._check_rating_sources([s for s, _ in min_ratings] + ([rating_sort] if rating_sort else []))
        for source, lowest in min_ratings:
            if source in BUILTIN_COLUMNS:
                conditions.append(f"s.{BUILTIN_COLUMNS[source]} >= ?")  # column names come from BUILTIN_COLUMNS only
                params.append(lowest)
            else:
                conditions.append(
                    "EXISTS (SELECT 1 FROM title_ratings r WHERE r.show_id = s.id AND r.source_id = ? AND r.value >= ?)")
                params.extend([source, lowest])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        if rating_sort:
            if rating_sort in BUILTIN_COLUMNS:
                query += f" ORDER BY s.{BUILTIN_COLUMNS[rating_sort]} DESC NULLS LAST, s.title ASC"
            else:
                query += (" ORDER BY (SELECT r.value FROM title_ratings r WHERE r.show_id = s.id AND r.source_id = ?)"
                          " DESC NULLS LAST, s.title ASC")
                params.append(rating_sort)
        elif sort_by in BUILTIN_COLUMNS:
            # a built-in source, then the built-in sources listed after it break ties (column names: BUILTIN_COLUMNS)
            columns = list(BUILTIN_COLUMNS.values())
            order = columns[columns.index(BUILTIN_COLUMNS[sort_by]):]
            query += " ORDER BY " + ", ".join(f"s.{c} DESC NULLS LAST" for c in order) + ", s.title ASC"
        elif sort_by == "my_rating":
            query += " ORDER BY s.user_rating DESC NULLS LAST, s.imdb_rating DESC NULLS LAST, s.title ASC"
        elif sort_by == "title":
            query += " ORDER BY s.title ASC"
        elif sort_by == "next_air":
            query += " ORDER BY s.next_episode_air_date ASC NULLS LAST, s.title ASC"
        elif sort_by == "recent":
            query += " ORDER BY s.last_watched_at DESC NULLS LAST, s.updated_at DESC"
        else:
            query += """
            ORDER BY
                CASE s.priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
                s.imdb_rating DESC NULLS LAST,
                s.title ASC
            """

        # Building validated Show models is most of the cost (~75 us a row, ~60 ms for the library), so the parsed
        # library is reused until the next write: the query only picks ids and order, and each caller gets copies.
        version = self._data_version
        by_id = await self.derived("shows_by_id", self._parse_all_shows)
        cursor = await db.execute(query.replace("SELECT s.*", "SELECT s.id", 1), params)
        ids = [r[0] for r in await cursor.fetchall()]
        if self._data_version == version and all(i in by_id for i in ids):
            return [by_id[i].model_copy() for i in ids]
        cursor = await db.execute(query, params)  # a write landed meanwhile: parse this result directly
        rows = await cursor.fetchall()
        all_ids = await title_ids_tx(db)
        shows = [self._row_to_show(r, all_ids.get(r["id"])) for r in rows]
        ratings = await self._all_title_ratings()
        for show in shows:
            show.ratings = ratings.get(show.id, {})
        return shows

    async def _parse_all_shows(self) -> Dict[int, Show]:
        db = await self.read_connection()
        cursor = await db.execute("SELECT * FROM shows")
        ratings = await self._all_title_ratings()
        all_ids = await title_ids_tx(db)
        parsed = {r["id"]: self._row_to_show(r, all_ids.get(r["id"]), trusted=True) for r in await cursor.fetchall()}
        for show_id, show in parsed.items():
            show.ratings = ratings.get(show_id, {})
        return parsed

    async def get_show_by_id(self, show_id: int) -> Optional[Show]:
        db = await self.read_connection()
        cursor = await db.execute("SELECT * FROM shows WHERE id = ?", (show_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        show = self._row_to_show(row, (await title_ids_tx(db, [show_id])).get(show_id))
        show.ratings = await self.get_title_ratings(show_id)
        return show

    async def count_titles(self) -> int:
        db = await self.read_connection()
        return (await (await db.execute("SELECT COUNT(*) FROM shows")).fetchone())[0]

    async def get_show_by_external_id(self, provider: str, external_id: Any) -> Optional[Show]:
        """The title with this provider's id (the oldest, if several titles share it)."""
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT show_id FROM title_ids WHERE provider = ? AND external_id = ? ORDER BY show_id LIMIT 1",
            (provider, external_id))
        row = await cursor.fetchone()
        return await self.get_show_by_id(row[0]) if row else None

    async def find_show_by_ids(self, ids: Dict[str, Any], providers: Optional[Iterable[str]] = None) -> Optional[Show]:
        """The stored title one of `ids` identifies, trying `providers` in order (default: key_providers)."""
        for provider in (self.key_providers if providers is None else providers):
            if ids.get(provider) is not None:
                found = await self.get_show_by_external_id(provider, ids[provider])
                if found:
                    return found
        return None

    async def _show_by_id_tx(self, db: aiosqlite.Connection, show_id: int) -> Optional[Show]:
        """get_show_by_id on the write transaction's own connection (sees the rows it has written)."""
        cursor = await db.execute("SELECT * FROM shows WHERE id = ?", (show_id,))
        row = await cursor.fetchone()
        return self._row_to_show(row, (await title_ids_tx(db, [show_id])).get(show_id)) if row else None

    async def shows_by_external_ids_tx(self, db: aiosqlite.Connection, provider: str,
                                       external_ids: Iterable[Any]) -> Dict[Any, Show]:
        """{external id: Show} for those ids of `provider` that are tracked, read on the write transaction's
        connection. If an id is stored for several titles, the oldest wins (what get_show_by_external_id returns)."""
        ids = list(dict.fromkeys(external_ids))
        found: Dict[Any, Show] = {}
        for start in range(0, len(ids), _MAX_IN_PARAMS):
            chunk = ids[start:start + _MAX_IN_PARAMS]
            cursor = await db.execute(
                f"SELECT t.external_id AS external_id, s.* FROM title_ids t JOIN shows s ON s.id = t.show_id "
                f"WHERE t.provider = ? AND t.external_id IN ({', '.join('?' for _ in chunk)}) ORDER BY s.id",
                [provider, *chunk])
            rows = await cursor.fetchall()
            all_ids = await title_ids_tx(db, [r["id"] for r in rows])
            for row in rows:
                if row["external_id"] not in found:
                    found[row["external_id"]] = self._row_to_show(row, all_ids.get(row["id"]))
        return found

    async def get_external_ids(self, provider: str, media_type: Optional[MediaType] = None) -> Dict[Any, int]:
        """{external id: local id} for the titles that have an id of `provider` (optionally of one media type)."""
        db = await self.read_connection()
        sql = "SELECT t.external_id, s.id FROM title_ids t JOIN shows s ON s.id = t.show_id WHERE t.provider = ?"
        params: List[Any] = [provider]
        if media_type:
            sql += " AND COALESCE(s.media_type, 'show') = ?"
            params.append(media_type.value)
        cursor = await db.execute(sql + " ORDER BY s.id DESC", params)  # the oldest title wins a shared id
        return {r[0]: r[1] for r in await cursor.fetchall()}

    async def upsert_show(self, show: Show) -> int:
        """Inserts a new show, or refreshes catalogue data of an existing one.

        Household-only fields (priority, household tag, notes, manual service pick)
        are never overwritten for existing rows; change them with update_show_metadata().
        """
        if not show.service_id:
            show.service_id = await self.match_service_by_network(show.network, title=show.title)
        async with self.write() as db:  # the existence check and the write are one transaction
            return await self._upsert_show_tx(db, show)

    async def _upsert_show_tx(self, db: aiosqlite.Connection, show: Show) -> int:
        """upsert_show inside an open write transaction (service matching is the caller's job)."""
        now = _utcnow()
        existing_id = None
        before = None
        columns = "id, status, user_rating"
        if show.id:
            r = await (await db.execute(f"SELECT {columns} FROM shows WHERE id = ?", (show.id,))).fetchone()
            if r:
                existing_id, before = r["id"], r
        else:
            for provider in self.key_providers:
                if show.ids.get(provider) is None:
                    continue
                r = await (await db.execute(
                    f"SELECT {', '.join('s.' + c for c in columns.split(', '))} FROM title_ids t "
                    "JOIN shows s ON s.id = t.show_id WHERE t.provider = ? AND t.external_id = ? ORDER BY s.id LIMIT 1",
                    (provider, show.ids[provider]))).fetchone()
                if r:
                    existing_id, before = r["id"], r
                    break

        if existing_id:
            params = []
            for col in SHOW_WRITE_COLUMNS:
                if col.update_sql:
                    params.extend([col.update_value(show, now)] * col.update_sql.count("?"))
            await db.execute(_SHOW_UPDATE_SQL, (*params, existing_id))
            await write_title_ids_tx(db, existing_id, show.ids)
            await refresh_search_text(db, existing_id)
            new_rating = show.user_rating if show.user_rating is not None else before["user_rating"]  # (keep if null)
            if self._event_listeners:
                self._queue_change_events(await title_ref_tx(db, existing_id), before["status"], show.status.value,
                                          before["user_rating"], new_rating)
            return existing_id

        cursor = await db.execute(_SHOW_INSERT_SQL, tuple(col.insert_value(show, now) for col in SHOW_WRITE_COLUMNS))
        show_id = cursor.lastrowid
        await write_title_ids_tx(db, show_id, show.ids)
        await refresh_search_text(db, show_id)
        self.queue_event("title.added", {"title": title_ref({
            "id": show_id, "title": show.title, "year": show.year,
            "media_type": (show.media_type or MediaType.SHOW).value}, show.ids), "status": show.status.value})
        return show_id

    def _queue_change_events(self, title: Optional[Dict[str, Any]], old_status: Optional[str],
                             new_status: Optional[str], old_rating: Optional[float],
                             new_rating: Optional[float]) -> None:
        """title.status_changed / title.rating_changed for a title written inside the open transaction (`title`:
        its events.title_ref)."""
        if not self._event_listeners or title is None:
            return
        if old_status != new_status and new_status is not None:
            self.queue_event("title.status_changed", {"title": title, "from": old_status, "to": new_status})
        if old_rating != new_rating:
            self.queue_event("title.rating_changed", {"title": title, "source": "user", "from": old_rating, "to": new_rating})

    async def update_show_fields(self, show_id: int, fields: Dict[str, Any]) -> None:
        """Writes only the given columns (whitelisted) and, under "ids", external ids. Used by the sync engine."""
        if not any(k in UPDATABLE_SHOW_COLUMNS for k in fields) and not fields.get("ids"):
            return
        async with self.write() as db:
            await self._update_show_fields_tx(db, show_id, fields)

    async def _update_show_fields_tx(self, db: aiosqlite.Connection, show_id: int, fields: Dict[str, Any]) -> None:
        """update_show_fields inside an open write transaction."""
        cols = {k: v for k, v in fields.items() if k in UPDATABLE_SHOW_COLUMNS}
        ids = fields.get("ids")
        if not cols and not ids:
            return
        if ids:
            await write_title_ids_tx(db, show_id, ids)
        if not cols:
            return
        before = None
        if self._event_listeners and ("status" in cols or "user_rating" in cols):
            before = await (await db.execute(
                "SELECT status, user_rating FROM shows WHERE id = ?", (show_id,))).fetchone()
        sets = ", ".join(f"{k} = ?" for k in cols)
        params = list(cols.values()) + [_utcnow(), show_id]
        await db.execute(f"UPDATE shows SET {sets}, updated_at = ? WHERE id = ?", params)
        if before is not None:
            self._queue_change_events(await title_ref_tx(db, show_id), before["status"],
                                      cols.get("status", before["status"]),
                                      before["user_rating"], cols.get("user_rating", before["user_rating"]))
        if SEARCH_SOURCE_COLUMNS & cols.keys():
            await refresh_search_text(db, show_id)

    async def get_show_season_size(self, show: Show) -> int:
        """Estimates episodes per season for a show (used only when the episode list is unknown)."""
        if show.id:
            db = await self.read_connection()
            cursor = await db.execute(
                """
                SELECT season, count(*) as ep_count
                FROM episodes
                WHERE show_id = ? AND season > 0
                GROUP BY season
                HAVING ep_count > 0
                ORDER BY count(*) DESC
                LIMIT 1
                """,
                (show.id,),
            )
            row = await cursor.fetchone()
            if row and row["ep_count"] > 0 and show.episodes_synced_at:
                return row["ep_count"]

        if show.media_type == MediaType.ANIME or "Anime" in show.genres:
            return 12
        if 0 < show.total_episodes <= 12:
            return show.total_episodes
        return 10

    async def update_show_progress(self, show_id: int, watched_episodes: int) -> Optional[Show]:
        now = _utcnow()
        async with self.write() as db:
            await db.execute(
                """
                UPDATE shows SET
                    watched_episodes = ?,
                    last_watched_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (watched_episodes, now, now, show_id),
            )
        return await self.get_show_by_id(show_id)

    async def increment_show_episode(self, show_id: int) -> Optional[Show]:
        show = await self.get_show_by_id(show_id)
        if not show:
            return None
        new_watched = show.watched_episodes + 1
        if show.aired_episodes > 0:
            new_watched = min(new_watched, show.aired_episodes)
        return await self.update_show_progress(show_id, new_watched)

    async def update_show_metadata(
        self,
        show_id: int,
        status: Optional[ShowStatus] = None,
        priority: Optional[Priority] = None,
        household_tag: Optional[HouseholdTag] = None,
        service_id: Optional[str] = None,
        network: Optional[str] = None,
        user_rating: Optional[float] = None,
        notes: Optional[str] = None,
        clear_user_rating: bool = False,
    ) -> Optional[Show]:
        updates = ["updated_at = ?"]
        params: List[Any] = [_utcnow()]

        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority.value)
        if household_tag is not None:
            updates.append("household_tag = ?")
            params.append(household_tag.value)
        if service_id is not None:
            # Empty string = "unassigned"; either way it's now a manual choice
            updates.append("service_id = ?")
            params.append(service_id or None)
            updates.append("service_manual = 1")
        if network is not None:
            updates.append("network = ?")
            params.append(network)
        if clear_user_rating:
            updates.append("user_rating = NULL")
        elif user_rating is not None:
            updates.append("user_rating = ?")
            params.append(user_rating)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)

        params.append(show_id)
        async with self.write() as db:
            before = None
            if self._event_listeners and (status is not None or clear_user_rating or user_rating is not None):
                before = await (await db.execute(
                    "SELECT status, user_rating FROM shows WHERE id = ?", (show_id,))).fetchone()
            await db.execute(f"UPDATE shows SET {', '.join(updates)} WHERE id = ?", params)
            if network is not None:
                await refresh_search_text(db, show_id)
            if before is not None:
                new_rating = None if clear_user_rating else (user_rating if user_rating is not None else before["user_rating"])
                self._queue_change_events(await title_ref_tx(db, show_id), before["status"],
                                          status.value if status is not None else before["status"],
                                          before["user_rating"], new_rating)
        return await self.get_show_by_id(show_id)

    async def delete_show(self, show_id: int) -> bool:
        async with self.write() as db:
            cursor = await db.execute("DELETE FROM shows WHERE id = ?", (show_id,))
            await db.execute("DELETE FROM sync_outbox WHERE show_id = ? AND op != 'remove'", (show_id,))
        return cursor.rowcount > 0

