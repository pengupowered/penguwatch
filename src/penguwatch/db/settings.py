"""The settings store: what the owner set on the Settings tab (penguwatch.settings), in three tables (migration 0019)
and five app_state keys.

- `settings_values`: {registry key: JSON value} for the keys that are set; a missing key means the default.
- `settings_items`: the items of the lists (only `webhooks`), one row per item (its id and position, the item as
  JSON; never a URL or a secret: those are slots in secrets.json).
- `settings_versions`: the whole document after every save (revision 1 is the one-time import), the last
  MAX_VERSIONS kept.
- app_state `settings_revision` (the saves so far), `settings_initialized` ("1": the one-time import ran, never
  cleared), `settings_lists_set` (JSON list of the list names that are set, not at their default; a set list may be
  empty), `settings_import_report` (JSON: what the import left out and why, never a value), `settings_imported_at`
  (epoch seconds), `settings_slot_revisions` (JSON {secret slot: the revision that last wrote or cleared it}, for the
  conflict check; slot names only, never a value).

A save is one write transaction: the conflict check, the rows, the new revision and its version. Rows of keys or
lists this build does not know (written by a newer one) are kept and never touched by a save. Nothing here ever holds
a secret.
"""

import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from penguwatch.settings.document import SettingsDocument

logger = logging.getLogger("penguwatch.db")

SETTINGS_REVISION_KEY = "settings_revision"
SETTINGS_INITIALIZED_KEY = "settings_initialized"
SETTINGS_LISTS_SET_KEY = "settings_lists_set"
SETTINGS_IMPORT_REPORT_KEY = "settings_import_report"
SETTINGS_IMPORTED_AT_KEY = "settings_imported_at"
SETTINGS_SLOT_REVISIONS_KEY = "settings_slot_revisions"  # {slot: the revision that last wrote it}: names only
MAX_VERSIONS = 50
SUMMARY_MAX = 300

_MISSING = object()


class SettingsConflict(Exception):
    """Parts a save touches changed since the revision it started from."""

    def __init__(self, current: int, conflicts: List[str]):
        super().__init__(f"the settings are at revision {current}")
        self.current = current
        self.conflicts = conflicts


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


async def _fetchall(db, sql: str, params: tuple = ()) -> list:
    cursor = await db.execute(sql, params)
    return list(await cursor.fetchall())


async def get_meta(db, key: str) -> Optional[str]:
    rows = await _fetchall(db, "SELECT value FROM app_state WHERE key = ?", (key,))
    return rows[0][0] if rows else None


async def put_meta(db, key: str, value: str) -> None:
    await db.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _json_list(raw: Optional[str]) -> list:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


async def _revision(db) -> int:
    raw = await get_meta(db, SETTINGS_REVISION_KEY)
    return int(raw) if raw and raw.isdigit() else 0


async def read_document(db) -> SettingsDocument:
    """Every stored row as a document, unknown keys and lists included (the manager leaves those out)."""
    values: Dict[str, Any] = {}
    for key, raw in await _fetchall(db, "SELECT key, value FROM settings_values ORDER BY key"):
        try:
            values[key] = json.loads(raw)
        except ValueError:
            logger.warning(f"Ignoring an unreadable stored setting: {key}")
    lists: Dict[str, Optional[List[dict]]] = {
        name: [] for name in _json_list(await get_meta(db, SETTINGS_LISTS_SET_KEY)) if isinstance(name, str)}
    for name, item_id, raw in await _fetchall(
            db, "SELECT list, id, config FROM settings_items ORDER BY list, position, id"):
        if name not in lists:
            continue  # rows of a list at its default are never written; ignore strays
        try:
            item = json.loads(raw)
        except ValueError:
            logger.warning(f"Ignoring an unreadable stored item: {name} {item_id}")
            continue
        if isinstance(item, dict):
            lists[name].append(item)
    return SettingsDocument(values=values, lists=lists)


async def _write(db, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]], now: float) -> None:
    """Writes the changed keys (None: removed, i.e. back to the default) and lists (None: back to the default)."""
    for key, value in values.items():
        if value is None:
            await db.execute("DELETE FROM settings_values WHERE key = ?", (key,))
        else:
            await db.execute("INSERT OR REPLACE INTO settings_values (key, value, updated) VALUES (?, ?, ?)",
                             (key, dumps(value), now))
    if not lists:
        return
    lists_set = [n for n in _json_list(await get_meta(db, SETTINGS_LISTS_SET_KEY)) if isinstance(n, str)]
    for name, items in lists.items():
        await db.execute("DELETE FROM settings_items WHERE list = ?", (name,))
        if items is None:
            lists_set = [n for n in lists_set if n != name]
            continue
        await db.executemany(
            "INSERT INTO settings_items (list, id, position, config, updated) VALUES (?, ?, ?, ?, ?)",
            [(name, str(item.get("id") or i), i, dumps(item), now) for i, item in enumerate(items)])
        if name not in lists_set:
            lists_set.append(name)
    await put_meta(db, SETTINGS_LISTS_SET_KEY, dumps(sorted(lists_set)))


async def _store_version(db, revision: int, source: str, summary: str, document: SettingsDocument,
                         now: float) -> None:
    summary = summary if len(summary) <= SUMMARY_MAX else summary[:SUMMARY_MAX - 1].rstrip() + "…"
    await db.execute(
        "INSERT OR REPLACE INTO settings_versions (revision, applied_at, source, summary, document) "
        "VALUES (?, ?, ?, ?, ?)", (revision, now, source, summary, dumps(document.to_json())))
    await db.execute("DELETE FROM settings_versions WHERE revision <= ?", (revision - MAX_VERSIONS,))


def _differs(a: SettingsDocument, b: SettingsDocument, keys: Iterable[str], lists: Iterable[str]) -> List[str]:
    out = [k for k in keys if a.values.get(k, _MISSING) != b.values.get(k, _MISSING)]
    out += [n for n in lists if a.lists.get(n) != b.lists.get(n)]
    return out


async def _slot_revisions(db) -> Dict[str, int]:
    try:
        data = json.loads(await get_meta(db, SETTINGS_SLOT_REVISIONS_KEY) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


async def _mark_slots(db, slots: Iterable[str], revision: int) -> None:
    written = await _slot_revisions(db)
    written.update({slot: revision for slot in slots})
    await put_meta(db, SETTINGS_SLOT_REVISIONS_KEY, dumps(written))


async def _version(db, revision: int) -> Optional[SettingsDocument]:
    rows = await _fetchall(db, "SELECT document FROM settings_versions WHERE revision = ?", (revision,))
    if not rows:
        return None
    try:
        data = json.loads(rows[0][0])
    except ValueError:
        return None
    return SettingsDocument.from_json(data) if isinstance(data, dict) else None


class SettingsMixin:
    """The repository's settings queries (DatabaseRepository)."""

    async def settings_initialized(self) -> bool:
        return await get_meta(await self.read_connection(), SETTINGS_INITIALIZED_KEY) == "1"

    async def settings_revision(self) -> int:
        return await _revision(await self.read_connection())

    async def load_settings(self) -> Tuple[int, SettingsDocument]:
        """The revision and the stored document (every row, unknown keys and lists included)."""
        db = await self.read_connection()
        return await _revision(db), await read_document(db)

    async def settings_import_info(self) -> Dict[str, Any]:
        """{"report": [{key, reason}], "imported_at": float | None}: what the one-time import left out, and when."""
        db = await self.read_connection()
        report = _json_list(await get_meta(db, SETTINGS_IMPORT_REPORT_KEY))
        at = await get_meta(db, SETTINGS_IMPORTED_AT_KEY)
        try:
            imported_at = float(at) if at else None
        except ValueError:
            imported_at = None
        return {"report": [r for r in report if isinstance(r, dict)], "imported_at": imported_at}

    async def initialize_settings(self, document: SettingsDocument, summary: str, report: List[Dict[str, str]],
                                  also: Optional[Callable[[Any], Awaitable[None]]] = None,
                                  slots: Iterable[str] = ()) -> bool:
        """The one-time import: `document` as revision 1 (source "import"), and `also(db)` (the services, rating
        sources and logins it hands over) in the same transaction. False, and nothing written, when the store was
        initialized before. An exception rolls everything back."""
        now = time.time()
        async with self.write() as db:
            if await get_meta(db, SETTINGS_INITIALIZED_KEY) == "1":
                return False
            if also is not None:
                await also(db)
            await _write(db, document.values, {n: v for n, v in document.lists.items() if v is not None}, now)
            if slots:
                await _mark_slots(db, slots, 1)
            await put_meta(db, SETTINGS_REVISION_KEY, "1")
            await _store_version(db, 1, "import", summary, await read_document(db), now)
            await put_meta(db, SETTINGS_INITIALIZED_KEY, "1")
            await put_meta(db, SETTINGS_IMPORT_REPORT_KEY, dumps(report))
            await put_meta(db, SETTINGS_IMPORTED_AT_KEY, repr(now))
        return True

    async def settings_conflicts(self, base_revision: int, keys: Iterable[str], lists: Iterable[str],
                                 slots: Iterable[str] = ()) -> Tuple[int, List[str]]:
        """(current revision, the touched parts that changed between `base_revision` and now)."""
        return await self._settings_conflicts(await self.read_connection(), base_revision, list(keys), list(lists),
                                              list(slots))

    @staticmethod
    async def _settings_conflicts(db, base_revision: int, keys: List[str], lists: List[str],
                                  slots: List[str] = ()) -> Tuple[int, List[str]]:
        """Every touched part is a conflict when the base version is no longer kept (or is in the future). A slot
        conflicts when a later revision wrote it."""
        current = await _revision(db)
        if current == base_revision:
            return current, []
        base = await _version(db, base_revision) if 0 < base_revision < current else None
        if base is None:
            return current, sorted(set(keys)) + sorted(set(lists)) + sorted(set(slots))
        written = await _slot_revisions(db)
        changed = [s for s in sorted(set(slots)) if written.get(s, 0) > base_revision]
        return current, _differs(base, await read_document(db), keys, lists) + changed

    async def save_settings(self, base_revision: int, values: Mapping[str, Any],
                            lists: Mapping[str, Optional[List[dict]]], source: str,
                            describe: Callable[[SettingsDocument, SettingsDocument], str],
                            slots: Iterable[str] = ()) -> Tuple[int, SettingsDocument, SettingsDocument, str]:
        """Applies a change (values[key] None: back to the default; lists[name] None: back to the default) as the
        next revision, unless a part it touches changed since `base_revision` (SettingsConflict). `slots`: the secret
        slots this save writes or clears (their names are kept for the conflict check; their values are in
        secrets.json). `describe(old, new)` gives the version's summary line. Returns (revision, document before,
        after, summary)."""
        now = time.time()
        slots = sorted(set(slots))
        async with self.write() as db:
            current, conflicts = await self._settings_conflicts(db, base_revision, list(values), list(lists), slots)
            if conflicts:
                raise SettingsConflict(current, conflicts)
            old = await read_document(db)
            await _write(db, values, lists, now)
            new = await read_document(db)
            summary = describe(old, new)
            if slots:
                await _mark_slots(db, slots, current + 1)
            await put_meta(db, SETTINGS_REVISION_KEY, str(current + 1))
            await put_meta(db, SETTINGS_INITIALIZED_KEY, "1")
            await _store_version(db, current + 1, source, summary, new, now)
        return current + 1, old, new, summary

    async def settings_versions(self) -> List[Dict[str, Any]]:
        """The kept versions, newest first: revision, applied_at, source, summary."""
        rows = await _fetchall(await self.read_connection(),
                               "SELECT revision, applied_at, source, summary FROM settings_versions "
                               "ORDER BY revision DESC LIMIT ?", (MAX_VERSIONS,))
        return [{"revision": r, "applied_at": at, "source": source, "summary": text} for r, at, source, text in rows]

    async def settings_version(self, revision: int) -> Optional[SettingsDocument]:
        """The document of a kept version, or None when it is not kept."""
        return await _version(await self.read_connection(), revision)
