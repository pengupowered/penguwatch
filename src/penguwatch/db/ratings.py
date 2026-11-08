"""Rating sources and the values users bring (custom sources), plus the filters and sort the library offers on them."""

from typing import Dict, Iterable, List, Optional, Tuple

import logging

import aiosqlite

from penguwatch.db._common import _utcnow
from penguwatch.db.shows import title_ids_tx
from penguwatch.drivers.base import BuiltinRatingSource
from penguwatch.events import title_ref
from penguwatch.ratings import (
    BUILTIN_COLUMNS, BUILTIN_SOURCES, RatingSource, RatingSourceSpec, RatingValue, TitleIndex, TitleRating,
)

logger = logging.getLogger("penguwatch.db")

_SOURCE_COLUMNS = "id, name, label, scale_min, scale_max, decimals, show_on_cards, position, origin"


MAX_SOURCES = 20  # custom sources in all, whether they come from config.yaml or the API


class UnknownRatingSource(LookupError):
    """A sort, filter or write named a rating source that does not exist."""


class InvalidScale(ValueError):
    """A scale whose minimum is not below its maximum."""


class RatingOutOfScale(ValueError):
    """Values lie outside the source's scale; `offenders` is [(show id, value)]."""

    def __init__(self, source: str, offenders):
        super().__init__(f"{len(offenders)} value(s) are outside the scale of '{source}'")
        self.offenders = offenders


def _source_from_row(r: aiosqlite.Row, rated: int = 0) -> RatingSource:
    return RatingSource(
        id=r["id"], name=r["name"], label=r["label"], scale_min=r["scale_min"], scale_max=r["scale_max"],
        decimals=r["decimals"], show_on_cards=bool(r["show_on_cards"]), position=r["position"],
        origin=r["origin"] if r["origin"] in ("builtin", "config", "api") else "api",
        builtin=r["origin"] == "builtin", read_only=r["origin"] in ("builtin", "config"), rated_titles=rated,
    )


async def import_rating_sources(db, specs: Iterable[RatingSourceSpec]) -> List[Dict[str, str]]:
    """The one-time settings import's hand-over of config.yaml's `ratings.sources`, inside the caller's write
    transaction: each source is created or updated as the old start-up seed did, except that one whose scale would
    leave stored values outside it is skipped and reported instead of stopping the start; then every source of
    origin 'config' becomes 'api' (editable on the Ratings tab, never seeded again). Returns [{key, reason}] for the
    skipped ones (never a value)."""
    report: List[Dict[str, str]] = []
    for index, spec in enumerate(specs):
        where = f"ratings.sources[{index}]"
        row = await (await db.execute("SELECT origin FROM rating_sources WHERE id = ?", (spec.id,))).fetchone()
        if row is not None and row["origin"] == "builtin":
            report.append({"key": where, "reason": "the id is a built-in rating source; left out"})
            continue
        outside = (await (await db.execute(
            "SELECT COUNT(*) AS n FROM title_ratings WHERE source_id = ? AND (value < ? OR value > ?)",
            (spec.id, spec.scale_min, spec.scale_max))).fetchone())["n"]
        if outside:
            report.append({"key": where, "reason": f"its scale would leave {outside} stored value(s) outside it; the "
                                                   "source was left as it is"})
            continue
        count = (await (await db.execute(
            "SELECT COUNT(*) AS n FROM rating_sources WHERE origin != 'builtin' AND id != ?", (spec.id,))).fetchone())["n"]
        if row is None and count >= MAX_SOURCES:
            report.append({"key": where, "reason": f"at most {MAX_SOURCES} custom rating sources; left out"})
            continue
        await db.execute(
            "INSERT INTO rating_sources (id, name, label, scale_min, scale_max, decimals, show_on_cards, position, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'api') "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, label = excluded.label, "
            "scale_min = excluded.scale_min, scale_max = excluded.scale_max, decimals = excluded.decimals, "
            "show_on_cards = excluded.show_on_cards, position = excluded.position, origin = 'api', "
            "updated_at = datetime('now')",
            (spec.id, spec.name, spec.label, spec.scale_min, spec.scale_max, spec.decimals,
             1 if spec.show_on_cards else 0, 2 + index),
        )
    await db.execute("UPDATE rating_sources SET origin = 'api' WHERE origin = 'config'")
    return report


class RatingsMixin:
    # --- sources ---

    async def get_rating_sources(self) -> List[RatingSource]:
        """Built-in sources first, then the custom ones by position; each with the number of titles it rates."""
        db = await self.read_connection()
        counts = {r["source_id"]: r["n"] for r in await (await db.execute(
            "SELECT source_id, COUNT(*) AS n FROM title_ratings GROUP BY source_id")).fetchall()}
        # the built-in sources' columns (names from BUILTIN_COLUMNS only), counted in one pass
        built: Dict[str, int] = {}
        if BUILTIN_COLUMNS:
            row = await (await db.execute("SELECT " + ", ".join(
                f"COUNT({column})" for column in BUILTIN_COLUMNS.values()) + " FROM shows")).fetchone()
            built = dict(zip(BUILTIN_COLUMNS, tuple(row), strict=True))
        out = [s.model_copy(update={"rated_titles": built.get(s.id, 0)}) for s in BUILTIN_SOURCES]
        rows = await (await db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM rating_sources ORDER BY origin != 'builtin', position, id")).fetchall()
        return out + [_source_from_row(r, counts.get(r["id"], 0)) for r in rows]

    async def get_rating_source(self, source_id: str) -> Optional[RatingSource]:
        for source in await self.get_rating_sources():
            if source.id == source_id:
                return source
        return None

    async def create_rating_source(self, spec: RatingSourceSpec, origin: str = "api") -> RatingSource:
        """Adds a custom source; ValueError when the id is taken."""
        async with self.write() as db:
            if await (await db.execute("SELECT 1 FROM rating_sources WHERE id = ?", (spec.id,))).fetchone():
                raise ValueError(f"a rating source '{spec.id}' already exists")
            if (await (await db.execute(
                    "SELECT COUNT(*) AS n FROM rating_sources WHERE origin != 'builtin'")).fetchone())["n"] >= MAX_SOURCES:
                raise ValueError(f"at most {MAX_SOURCES} custom rating sources; delete one first")
            last = await (await db.execute("SELECT MAX(position) AS p FROM rating_sources")).fetchone()
            position = max(2, (last["p"] if last and last["p"] is not None else 1) + 1)
            await db.execute(
                "INSERT INTO rating_sources (id, name, label, scale_min, scale_max, decimals, show_on_cards, position, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (spec.id, spec.name, spec.label, spec.scale_min, spec.scale_max, spec.decimals,
                 1 if spec.show_on_cards else 0, position, origin),
            )
        return await self.get_rating_source(spec.id)

    async def update_rating_source(self, source_id: str, changes: Dict[str, object]) -> Optional[RatingSource]:
        """Changes the given fields of a custom source (None if it does not exist). ValueError when the new scale
        would leave stored values outside it, or is not ordered."""
        async with self.write() as db:
            row = await (await db.execute(f"SELECT {_SOURCE_COLUMNS} FROM rating_sources WHERE id = ?", (source_id,))).fetchone()
            if row is None:
                return None
            merged = {k: row[k] for k in ("name", "label", "scale_min", "scale_max", "decimals", "show_on_cards", "position")}
            merged.update({k: v for k, v in changes.items() if v is not None})
            if not merged["scale_min"] < merged["scale_max"]:
                raise InvalidScale("scale_min must be less than scale_max")
            outside = await (await db.execute(
                "SELECT COUNT(*) AS n FROM title_ratings WHERE source_id = ? AND (value < ? OR value > ?)",
                (source_id, merged["scale_min"], merged["scale_max"]))).fetchone()
            if outside["n"]:
                raise ValueError(f"{outside['n']} stored value(s) lie outside the new scale; clear or re-import them first")
            await db.execute(
                "UPDATE rating_sources SET name = ?, label = ?, scale_min = ?, scale_max = ?, decimals = ?, "
                "show_on_cards = ?, position = ?, updated_at = ? WHERE id = ?",
                (merged["name"], merged["label"], merged["scale_min"], merged["scale_max"], merged["decimals"],
                 1 if merged["show_on_cards"] else 0, merged["position"], _utcnow(), source_id),
            )
        return await self.get_rating_source(source_id)

    async def delete_rating_source(self, source_id: str) -> bool:
        """Removes a custom source and, by cascade, every value it holds."""
        async with self.write() as db:
            removed = (await (await db.execute(
                "SELECT COUNT(*) AS n FROM title_ratings WHERE source_id = ?", (source_id,))).fetchone())["n"]
            cursor = await db.execute("DELETE FROM rating_sources WHERE id = ?", (source_id,))
            if cursor.rowcount:
                self.queue_event("ratings.cleared", {"source": source_id, "removed": removed, "source_deleted": True})
        return cursor.rowcount > 0

    async def seed_builtin_rating_sources(self, specs: Iterable[BuiltinRatingSource]) -> None:
        """Makes the rows exist for the built-in sources that have no column (those of the drivers config.yaml runs):
        their settings follow the driver. Values are never touched; a source whose driver was removed from the
        configuration keeps its values and stays listed.

        A row with the id that is not built-in is a custom source (made on an older version, before the id was
        reserved): it is left entirely alone and the driver's rating stays off (`rating_source_conflicts`)."""
        async with self.write() as db:
            for index, spec in enumerate(specs):
                row = await (await db.execute("SELECT origin FROM rating_sources WHERE id = ?", (spec.id,))).fetchone()
                if row is not None and row["origin"] != "builtin":
                    logger.warning(self.rating_conflict_message(spec))
                    continue
                await db.execute(
                    "INSERT INTO rating_sources (id, name, label, scale_min, scale_max, decimals, show_on_cards, position, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'builtin') "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, label = excluded.label, "
                    "scale_min = excluded.scale_min, scale_max = excluded.scale_max, decimals = excluded.decimals, "
                    "show_on_cards = excluded.show_on_cards, position = excluded.position, "
                    "updated_at = datetime('now')",
                    (spec.id, spec.name, spec.label, spec.scale_min, spec.scale_max, spec.decimals,
                     1 if spec.show_on_cards else 0, index),
                )

    @staticmethod
    def rating_conflict_message(spec: BuiltinRatingSource) -> str:
        return (f"The rating source '{spec.id}' is a custom source of yours, so the built-in {spec.name} rating is off "
                f"and your values are left alone. To use the built-in rating, rename or delete your '{spec.id}' "
                f"source, then restart.")

    async def rating_source_conflicts(self, specs: Iterable[BuiltinRatingSource]) -> List[str]:
        """Messages for the built-in sources (of the running drivers) whose id is taken by a custom source."""
        db = await self.read_connection()
        origins = {r["id"]: r["origin"] for r in await (await db.execute(
            "SELECT id, origin FROM rating_sources")).fetchall()}
        return [self.rating_conflict_message(spec) for spec in specs
                if origins.get(spec.id, "builtin") != "builtin"]

    async def builtin_rating_source_ids(self) -> set:
        """The ids of the sources whose row says built-in: the only ones a driver's ratings may be written to."""
        db = await self.read_connection()
        return {r["id"] for r in await (await db.execute(
            "SELECT id FROM rating_sources WHERE origin = 'builtin'")).fetchall()}

    async def seed_rating_sources(self, specs: Iterable[RatingSourceSpec]) -> None:
        """Makes the sources of config.yaml exist (their settings follow the file). Values are never touched. A source
        the file no longer lists keeps its values and becomes editable through the API."""
        specs = list(specs)
        async with self.write() as db:
            for index, spec in enumerate(specs):
                outside = (await (await db.execute(
                    "SELECT COUNT(*) AS n FROM title_ratings WHERE source_id = ? AND (value < ? OR value > ?)",
                    (spec.id, spec.scale_min, spec.scale_max))).fetchone())["n"]
                if outside:
                    raise ValueError(
                        f"config.yaml ratings.sources '{spec.id}': the scale {spec.scale_min:g} to {spec.scale_max:g} would leave "
                        f"{outside} stored value(s) outside it; widen the scale or clear that source's values first")
                await db.execute(
                    "INSERT INTO rating_sources (id, name, label, scale_min, scale_max, decimals, show_on_cards, position, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'config') "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, label = excluded.label, "
                    "scale_min = excluded.scale_min, scale_max = excluded.scale_max, decimals = excluded.decimals, "
                    "show_on_cards = excluded.show_on_cards, position = excluded.position, origin = 'config', "
                    "updated_at = datetime('now')",
                    (spec.id, spec.name, spec.label, spec.scale_min, spec.scale_max, spec.decimals,
                     1 if spec.show_on_cards else 0, 2 + index),
                )
            marks = ", ".join("?" for _ in specs)
            await db.execute(
                f"UPDATE rating_sources SET origin = 'api' WHERE origin = 'config'{f' AND id NOT IN ({marks})' if specs else ''}",
                [s.id for s in specs],
            )

    # --- values ---

    @staticmethod
    def _rating_from_row(r: aiosqlite.Row) -> TitleRating:
        return TitleRating(value=r["value"], votes=r["votes"], url=r["url"], updated_at=r["updated_at"])

    async def _all_title_ratings(self) -> Dict[int, Dict[str, TitleRating]]:
        db = await self.read_connection()
        grouped: Dict[int, Dict[str, TitleRating]] = {}
        cursor = await db.execute("SELECT show_id, source_id, value, votes, url, updated_at FROM title_ratings ORDER BY show_id, source_id")
        for r in await cursor.fetchall():
            grouped.setdefault(r["show_id"], {})[r["source_id"]] = self._rating_from_row(r)
        return grouped

    async def get_title_ratings(self, show_id: int) -> Dict[str, TitleRating]:
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT source_id, value, votes, url, updated_at FROM title_ratings WHERE show_id = ? ORDER BY source_id", (show_id,))
        return {r["source_id"]: self._rating_from_row(r) for r in await cursor.fetchall()}

    async def title_index(self) -> TitleIndex:
        db = await self.read_connection()
        cursor = await db.execute("SELECT id, title, en_title, alt_titles_json, year, media_type FROM shows")
        rows = [dict(r) for r in await cursor.fetchall()]
        all_ids = await title_ids_tx(db)
        for row in rows:
            row["ids"] = all_ids.get(row["id"], {})
        return TitleIndex(rows)

    async def set_title_ratings(self, source_id: str, values: Dict[int, RatingValue],
                                events: str = "each") -> Tuple[Dict[int, Optional[float]], int]:
        """Stores one value per title for a custom source (replacing value, votes and link). Returns {id: previous
        value or None} for the titles whose stored rating changed, and how many were already as given.

        The scale is checked here, inside the write transaction, against the source as it is now (RatingOutOfScale;
        UnknownRatingSource when the source is gone). `events`: "each" raises title.rating_changed per changed
        title, "summary" one ratings.imported for the batch."""
        if not values:
            return {}, 0
        changed: Dict[int, Optional[float]] = {}
        unchanged = 0
        async with self.write() as db:
            src = await (await db.execute(
                "SELECT scale_min, scale_max FROM rating_sources WHERE id = ?", (source_id,))).fetchone()
            if src is None:
                raise UnknownRatingSource(f"unknown rating source: {source_id}")
            bad = [(i, v.value) for i, v in values.items() if not src["scale_min"] <= v.value <= src["scale_max"]]
            if bad:
                raise RatingOutOfScale(source_id, bad)
            existing = {r["show_id"]: (r["value"], r["votes"], r["url"]) for r in await (await db.execute(
                "SELECT show_id, value, votes, url FROM title_ratings WHERE source_id = ?", (source_id,))).fetchall()}
            present = {r["id"] for r in await (await db.execute("SELECT id FROM shows")).fetchall()}
            now = _utcnow()
            rows = []
            for show_id, v in values.items():
                if show_id not in present:
                    continue
                new = (v.value, v.votes, v.url)
                if existing.get(show_id) == new:
                    unchanged += 1
                    continue
                changed[show_id] = existing[show_id][0] if show_id in existing else None
                rows.append((show_id, source_id, v.value, v.votes, v.url, now))
            await db.executemany(
                "INSERT INTO title_ratings (show_id, source_id, value, votes, url, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(show_id, source_id) DO UPDATE SET value = excluded.value, votes = excluded.votes, "
                "url = excluded.url, updated_at = excluded.updated_at",
                rows,
            )
            if changed and events == "summary":
                self.queue_event("ratings.imported", {"source": source_id, "updated": len(changed), "unchanged": unchanged})
            elif changed and events == "each" and self._event_listeners:
                ids = list(changed)
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    marks = ", ".join("?" for _ in chunk)
                    all_ids = await title_ids_tx(db, chunk)
                    for ref in await (await db.execute(
                            f"SELECT id, title, year, media_type FROM shows WHERE id IN ({marks})", chunk)).fetchall():
                        self.queue_event("title.rating_changed", {
                            "title": title_ref(ref, all_ids.get(ref["id"])), "source": source_id, "from": changed[ref["id"]],
                            "to": values[ref["id"]].value})
        return changed, unchanged

    async def clear_source_ratings(self, source_id: str) -> int:
        async with self.write() as db:
            cursor = await db.execute("DELETE FROM title_ratings WHERE source_id = ?", (source_id,))
            if cursor.rowcount:
                self.queue_event("ratings.cleared", {"source": source_id, "removed": cursor.rowcount, "source_deleted": False})
        return cursor.rowcount

    async def delete_title_rating(self, source_id: str, show_id: int) -> bool:
        async with self.write() as db:
            old = await (await db.execute(
                "SELECT r.value, s.id, s.title, s.year, s.media_type FROM title_ratings r JOIN shows s ON s.id = r.show_id "
                "WHERE r.source_id = ? AND r.show_id = ?", (source_id, show_id))).fetchone()
            cursor = await db.execute("DELETE FROM title_ratings WHERE source_id = ? AND show_id = ?", (source_id, show_id))
            if old is not None:
                ref = title_ref(old, (await title_ids_tx(db, [show_id])).get(show_id))
                self.queue_event("title.rating_changed", {"title": ref, "source": source_id, "from": old["value"], "to": None})
        return cursor.rowcount > 0

    # --- sort and filter ---

    async def _check_rating_sources(self, ids: Iterable[str]) -> None:
        wanted = {i for i in ids if i not in BUILTIN_COLUMNS}
        if not wanted:
            return
        known = {s.id for s in await self.get_rating_sources()}
        missing = sorted(wanted - known)
        if missing:
            raise UnknownRatingSource(f"unknown rating source: {missing[0]}")
