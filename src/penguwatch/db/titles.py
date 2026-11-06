"""Alternative names of a title (English title, the catalogue's alternative titles) and the search text built from
them."""

import json
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import aiosqlite

from penguwatch.textsearch import build_search_text, clean_alt_titles, clean_en_title, compact, normalize

# Columns whose change makes the stored search text stale.
SEARCH_SOURCE_COLUMNS = frozenset({"title", "en_title", "alt_titles_json", "network", "genres_json"})


def _loads(raw: Optional[str]) -> List[Any]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def search_text_for_row(row: Any) -> str:
    """From a (title, en_title, alt_titles_json, network, genres_json) row, in that order."""
    title, en_title, alt_json, network, genres_json = row
    return build_search_text(
        title, en_title, clean_alt_titles(_loads(alt_json)), network,
        [g for g in _loads(genres_json) if isinstance(g, str)],
    )


async def refresh_search_text(db: aiosqlite.Connection, show_id: int) -> None:
    """Recomputes shows.search_text for one title. Call inside a write() block after changing a source column."""
    cursor = await db.execute(
        "SELECT title, en_title, alt_titles_json, network, genres_json FROM shows WHERE id = ?", (show_id,)
    )
    row = await cursor.fetchone()
    if row is not None:
        await db.execute("UPDATE shows SET search_text = ? WHERE id = ?", (search_text_for_row(row), show_id))


def search_clause(query: str) -> Tuple[str, List[str]]:
    """SQL condition (and parameters) matching `query` the forgiving way, or the old raw LIKE alone when the
    query has nothing left after normalizing (only punctuation)."""
    raw = f"%{query.strip()}%"
    sql = "(s.title LIKE ? OR s.network LIKE ? OR s.genres_json LIKE ?"
    params = [raw, raw, raw]
    spaced, packed = normalize(query), compact(query)
    if packed:
        sql += " OR s.search_text LIKE ? OR s.search_text LIKE ?"
        params += [f"%{spaced}%", f"%{packed}%"]
    return sql + ")", params


class TitlesMixin:
    async def update_show_titles(self, show_id: int, en_title: Optional[str], alt_titles: List[str]) -> None:
        """Stores what the catalogue says the title is called elsewhere. An empty list still marks the title as checked."""
        async with self.write() as db:
            cursor = await db.execute("SELECT title FROM shows WHERE id = ?", (show_id,))
            row = await cursor.fetchone()
            if row is None:
                return
            await db.execute(
                "UPDATE shows SET en_title = ?, alt_titles_json = ? WHERE id = ?",
                (clean_en_title(row["title"], en_title), json.dumps(clean_alt_titles(alt_titles)), show_id),
            )
            await refresh_search_text(db, show_id)

    async def get_unchecked_title_ids(self, providers: Iterable[str]) -> Set[int]:
        """Ids of titles with an id of one of `providers` whose alternative titles have never been fetched."""
        providers = list(providers)
        if not providers:
            return set()
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT id FROM shows s WHERE alt_titles_json IS NULL AND EXISTS (SELECT 1 FROM title_ids t "
            f"WHERE t.show_id = s.id AND t.provider IN ({', '.join('?' for _ in providers)}))", providers)
        return {r["id"] for r in await cursor.fetchall()}

    async def get_en_titles(self, show_ids: Iterable[int]) -> Dict[int, str]:
        """{show id: English title} for the given titles that have one."""
        ids = list(dict.fromkeys(show_ids))
        if not ids:
            return {}
        db = await self.read_connection()
        marks = ", ".join("?" for _ in ids)
        cursor = await db.execute(f"SELECT id, en_title FROM shows WHERE en_title IS NOT NULL AND id IN ({marks})", ids)
        return {r["id"]: r["en_title"] for r in await cursor.fetchall()}
