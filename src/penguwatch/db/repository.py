"""Database repository for PenguWatch."""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar
import aiosqlite

from homeguard.db import Database, ReadSession
from penguwatch.interest import build_profile
from penguwatch.models import ServiceConfig, ShowStatus
from penguwatch.db.migrations import run_migrations
from penguwatch.db._common import _utcnow
from penguwatch.db.services import ServicesMixin, seed_services
from penguwatch.db.settings import SettingsMixin
from penguwatch.db.prices import PricesMixin, record_price
from penguwatch.db.shows import ShowsMixin, UPDATABLE_SHOW_COLUMNS
from penguwatch.db.titles import TitlesMixin
from penguwatch.db.episodes import EpisodesMixin, format_episode_code
from penguwatch.db.views import ViewsMixin
from penguwatch.db.outbox import OutboxMixin
from penguwatch.db.state import StateMixin
from penguwatch.db.reports import ReportsMixin
from penguwatch.db.history import HistoryMixin
from penguwatch.db.ratings import RatingsMixin
from penguwatch.db.events import EventsMixin
from penguwatch.drivers import DEFAULT_LIBRARY

logger = logging.getLogger("penguwatch.db")

T = TypeVar("T")


class DatabaseRepository(
    ServicesMixin, PricesMixin, ShowsMixin, TitlesMixin, EpisodesMixin, ViewsMixin, OutboxMixin, StateMixin, ReportsMixin,
    RatingsMixin, EventsMixin, HistoryMixin, SettingsMixin,
):
    def __init__(self, db_path: str, key_providers: Tuple[str, ...] = (DEFAULT_LIBRARY,)):
        self.db_path = db_path
        self.key_providers = tuple(key_providers)
        # One writer connection (write() takes turns, so one coroutine's commit never commits another's
        # half-done work) and one read-only connection, which never sees uncommitted rows. WAL, so reads
        # and the writer do not block each other.
        self._database = Database(
            db_path, pragmas=("synchronous=NORMAL",), wal=True, row_factory=aiosqlite.Row, foreign_keys=True
        )
        # Derived data (e.g. the taste profile) is cached against this counter, which every write transaction bumps.
        self._data_version = 0
        self._derived: Dict[str, Tuple[int, Any]] = {}
        self._event_listeners = []
        self._pending_events = []

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """One logical write transaction: exclusive, committed on success, rolled back on error.

        Every statement that changes data goes through here. Reads use read_connection() and never wait for it
        (nor see its uncommitted rows: do not read back what the block wrote through a repository read method,
        use `db`). Not re-entrant: do not call another write method (or write()) from inside the block.
        """
        committed = False
        try:
            async with self._database.write() as db:
                yield db
            committed = True
        finally:
            self._data_version += 1  # after the commit; also after an error, as an explicit commit may have run
            self._publish_pending(committed)  # events queued by the block go out only if it committed

    async def derived(self, key: str, build: Callable[[], Awaitable[T]]) -> T:
        """A value computed from the database, reused until the next write transaction (single writer process)."""
        hit = self._derived.get(key)
        if hit is not None and hit[0] == self._data_version:
            return hit[1]
        version = self._data_version
        value = await build()
        self._derived[key] = (version, value)
        return value

    async def taste_profile(self):
        """The interest profile of the whole library, rebuilt only after a write."""
        return await self.derived("taste_profile", lambda: self._build_taste_profile())

    async def _build_taste_profile(self):
        # Only what the profile reads, without parsing whole rows into validated models (same order as the
        # default library listing, so the float sums come out identical).
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT status, watched_episodes, user_rating, genres_json FROM shows "
            "ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "imdb_rating DESC NULLS LAST, title ASC"
        )
        return build_profile(
            SimpleNamespace(  # duck-typed Show: just the fields the profile reads
                status=ShowStatus(r["status"]) if r["status"] else ShowStatus.PLANTOWATCH,
                watched_episodes=r["watched_episodes"] or 0,
                user_rating=r["user_rating"],
                genres=json.loads(r["genres_json"]) if r["genres_json"] else [],
            )
            for r in await cursor.fetchall()
        )

    async def initialize(self, default_services: Optional[List[ServiceConfig]] = None):
        """Creates the database file's folder and runs the pending migrations. The app seeds nothing here any more:
        the one-time settings import hands config.yaml's services over once (penguwatch.settings.importer), and the
        catalogue refreshes the untouched ones. `default_services` is for tests only: those rows are upserted as the
        import would (db.services.seed_services, not marked edited)."""
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self.write() as db:
            await run_migrations(db)
            if default_services:
                await seed_services(db, default_services, user_edited=False)
            await db.execute("PRAGMA optimize")  # refreshes the planner's statistics where they have gone stale
        self._harden_file_modes()

    def _harden_file_modes(self) -> None:
        """The database holds credentials: the file and its WAL/SHM siblings are for the app's user only."""
        if self.db_path == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.db_path + suffix, 0o600)
            except OSError:
                pass  # absent (no WAL yet) or a filesystem without modes

    async def get_connection(self) -> aiosqlite.Connection:
        """The raw writer connection, without the write lock or the commit: for the in-memory export and tests.
        Application writes use write(); reads use read_connection()."""
        return await self._database.writer()

    async def read_connection(self) -> ReadSession:
        """The read-only connection every read goes through (fetches whole results, sees committed rows only)."""
        return await self._database.reader()

    async def close(self):
        await self._database.close()
