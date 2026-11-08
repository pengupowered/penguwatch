"""Numbered schema migrations.

Each migration lives in a module named ``NNNN_<name>.py`` exposing ``async def migrate(conn)``.
Modules start with a digit, so they are loaded with ``importlib.import_module`` by string.
The runner applies pending ones in order, one transaction each, recording them in
``schema_migrations``. Migrations must not use ``executescript`` (it commits implicitly).
"""

import importlib
import logging
from typing import Awaitable, Callable, List, Tuple

import aiosqlite

logger = logging.getLogger("penguwatch.db")

Migration = Callable[[aiosqlite.Connection], Awaitable[None]]

_PACKAGE = "penguwatch.db.migrations"
_REGISTRY = [
    (1, "initial", "0001_initial"),
    (2, "legacy_columns", "0002_legacy_columns"),
    (3, "episode_watched_index", "0003_episode_watched_index"),
    (4, "service_bundles", "0004_service_bundles"),
    (5, "billing_period", "0005_billing_period"),
    (6, "service_price_history", "0006_service_price_history"),
    (7, "english_titles", "0007_english_titles"),
    (8, "drop_tmdb_state", "0008_drop_tmdb_state"),
    (9, "clear_stale_providers", "0009_clear_stale_providers"),
    (10, "neutral_household_tags", "0010_neutral_household_tags"),
    (11, "drop_rotten_tomatoes", "0011_drop_rotten_tomatoes"),
    (12, "service_titles", "0012_service_titles"),
    (13, "drop_partner_rating", "0013_drop_partner_rating"),
    (14, "rating_sources", "0014_rating_sources"),
    (15, "title_ids", "0015_title_ids"),
    (16, "outbox_ops", "0016_outbox_ops"),
    (17, "driver_state", "0017_driver_state"),
    (18, "staged_cursor", "0018_staged_cursor"),
    (19, "settings", "0019_settings"),
]

MIGRATIONS: List[Tuple[int, str, Migration]] = [
    (version, name, importlib.import_module(f"{_PACKAGE}.{module}").migrate)
    for version, name, module in _REGISTRY
]


async def run_migrations(conn: aiosqlite.Connection) -> List[int]:
    """Apply pending migrations in order, each in its own transaction.

    Returns the versions applied. Creates ``schema_migrations`` if missing.
    """
    if conn.in_transaction:
        await conn.commit()
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    await conn.commit()
    cursor = await conn.execute("SELECT version FROM schema_migrations")
    done = {row[0] for row in await cursor.fetchall()}

    applied: List[int] = []
    for version, name, fn in MIGRATIONS:
        if version in done:
            continue
        await conn.execute("BEGIN")
        try:
            await fn(conn)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, datetime('now'))",
                (version, name),
            )
            await conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise
        applied.append(version)
        logger.info(f"Applied database migration {version:04d}_{name}")
    return applied
