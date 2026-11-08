"""Data portability: CSV export of the library and a credential-free database backup."""

import asyncio
import csv
import io
import os
import sqlite3
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Response

from penguwatch.api.deps import get_repo
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import DRIVERS
from penguwatch.drivers.base import state_key
from penguwatch.ratings import csv_column
from penguwatch.security import rate_limit

router = APIRouter(tags=["Export"])

# Every driver's credentials (its secret_state_keys under its namespace): never in a backup
SECRET_STATE_KEYS = tuple(state_key(name, key) for name, driver in DRIVERS.items() for key in driver.secret_state_keys)
# Caches of what services answered: refetched when needed, so a backup leaves them out
CACHE_STATE_SQL = "key LIKE 'trending:%' OR key = 'discover.recommendations'"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# External ids with a column of their own (<provider>_id), then the library driver's id (repository.key_providers)
CSV_ID_PROVIDERS = ("imdb", "tmdb")


def _cell(value) -> str:
    """Titles come from a third party: neutralise spreadsheet formula injection (=HYPERLINK(...))."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)  # a number is not a formula, and a custom scale may start below zero
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


@router.get("/export/library.csv", summary="Export the library as CSV")
async def export_library_csv(repo: DatabaseRepository = Depends(get_repo)):
    shows = await repo.get_shows(sort_by="title")
    services = {s.id: s.name for s in await repo.get_services()}
    custom = [s for s in await repo.get_rating_sources() if not s.builtin]  # one column per custom source
    id_providers = list(dict.fromkeys([*CSV_ID_PROVIDERS, *repo.key_providers]))
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["title", "year", "type", "status", "priority", "audience", "service", "network", "our_rating",
                  "imdb_rating", "episodes_watched", "episodes_aired", "episodes_total",
                  "runtime_minutes", "genres", *(f"{p}_id" for p in id_providers), "last_watched_at",
                  *(csv_column(s.id) for s in custom)])
    for s in shows:
        out.writerow([_cell(v) for v in (
            s.title, s.year, s.media_type.value, s.status.value, s.priority.value, s.household_tag.value,
            services.get(s.service_id or "", s.service_id or ""), s.network, s.user_rating,
            s.imdb_rating, s.watched_episodes, s.aired_episodes, s.total_episodes, s.runtime_minutes,
            "; ".join(s.genres), *(s.ids.get(p) for p in id_providers), s.last_watched_at,
            *(s.ratings[c.id].value if c.id in s.ratings else None for c in custom),
        )])
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="penguwatch-library.csv"'},
    )


def _strip_secrets(path: str) -> None:
    """The drivers' logins and the caches out of a backup copy. The Settings tab's secrets (the digest address, the
    webhooks' addresses and signing secrets) are not in the database at all: they live in secrets.json next to it
    (penguwatch.secrets), which this download never includes; `penguwatch backup` copies it (the private full
    backup). The settings tables hold no secret, so they stay in the download."""
    conn = sqlite3.connect(path)
    try:
        marks = ",".join("?" * len(SECRET_STATE_KEYS))
        conn.execute(f"DELETE FROM app_state WHERE key IN ({marks})", SECRET_STATE_KEYS)
        conn.execute(f"DELETE FROM app_state WHERE {CACHE_STATE_SQL}")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _snapshot(db_path: str, target: str) -> None:
    """VACUUM INTO on its own short-lived connection: WAL readers do not block the app's connection (or it them)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (target,))
    finally:
        conn.close()


@router.get("/export/backup.db", summary="Download a database backup", dependencies=[Depends(rate_limit("backup", 3, 60))])
async def export_backup(repo: DatabaseRepository = Depends(get_repo)):
    """A consistent snapshot of the database (SQLite VACUUM INTO) without the drivers' credentials."""
    tmpdir = tempfile.mkdtemp(prefix="wt-backup-")
    target = os.path.join(tmpdir, "penguwatch-backup.db")
    try:
        if repo.db_path == ":memory:":  # a second connection would see an empty database
            await (await repo.get_connection()).execute("VACUUM INTO ?", (target,))
        else:
            await asyncio.to_thread(_snapshot, repo.db_path, target)
        await asyncio.to_thread(_strip_secrets, target)
        data = Path(target).read_bytes()
    finally:
        for name in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)
    return Response(
        data,
        media_type="application/vnd.sqlite3",
        headers={"Content-Disposition": 'attachment; filename="penguwatch-backup.db"'},
    )
