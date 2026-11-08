"""`penguwatch settings check-import CONFIG DB [--live]`: a dry run of the one-time settings import, for the deploy.

The database (and the secrets.json next to it, when there is one) is copied into a private temporary folder first:
the database through a read-only connection with SQLite's backup API, so the file given is never written (nor its
WAL). The copy is migrated and imported exactly as the app does at its first start (without --live; with --live the
copy must already be imported, and is only read), then compared key by key with what the build before the settings
store ran on: the file's value where it sets a key, else that build's default (penguwatch.settings.legacy). The
services table is compared with the file's `services:` list, the webhooks with the file's.

It never builds a driver and never opens a network connection: the copy holds the owner's tracker login and outbox,
and a started app would pull and push against the real account. The output names keys, lists and counts only:
`same`, `differs`, `skipped (<reason>)`; never a value, so it is safe to paste. Exit 0 when everything came across
unchanged, 1 when something differs or was skipped, 2 when the check could not run. The copies are deleted at the
end.
"""

import asyncio
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from penguwatch import paths
from penguwatch.config import AppConfig, EnvOverrides, read_config_file
from penguwatch.settings import legacy, lenient, registry
from penguwatch.settings.importer import get_path, jsonable, present, read_settings, refresh_catalogue


def _copy_db(source: Path, target: Path) -> None:
    """A consistent copy through a read-only connection: the source (and its WAL) is not written. Without a WAL next to
    it (a backup, a stopped app) the file is opened as immutable, so not even -shm / -wal files appear next to it."""
    mode = "mode=ro" if source.with_name(source.name + "-wal").exists() else "mode=ro&immutable=1"
    src = sqlite3.connect(f"file:{quote(str(source.resolve()))}?{mode}", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(target, 0o600)


def _schema_version(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def no_environment() -> EnvOverrides:
    """The variables as if none were set: the check compares the file with the store, not with this shell."""
    return EnvOverrides.model_construct(driver_values={})


def old_effective(raw: dict, config_path: Optional[str]) -> Dict[str, Any]:
    """{registry key: what the build before the settings store ran with}: the file's value where it sets one the
    models accept, else that build's default (its example's, for an install that ran on the example)."""
    data = {k: v for k, v in raw.items() if k not in ("services", "ratings", "webhooks")}
    config, _ = lenient.validate_leniently(AppConfig, data)
    old = legacy.old_values(legacy.ran_on_example(config_path))
    return {s.key: jsonable(get_path(config, s.key)) if present(data, s.key) else old.get(s.key)
            for s in registry.SETTINGS}


async def _run(config_path: Path, raw: dict, db_copy: Path, secrets_copy: Path, live: bool,
               out: Callable[[str], None]) -> int:
    from penguwatch.db.repository import DatabaseRepository
    from penguwatch.secrets import SecretStore
    from penguwatch.settings.hub import SettingsHub
    from penguwatch.settings.manager import SettingsManager

    before = _schema_version(db_copy)
    started = time.monotonic()
    repository = DatabaseRepository(db_path=str(db_copy))
    try:
        await repository.initialize()
        out(f"migration: {before} -> {_schema_version(db_copy)} on the copy in {time.monotonic() - started:.1f} s")
        if live and not await repository.settings_initialized():
            out("the database has no imported settings yet (nothing to check with --live)")
            return 2
        store = SecretStore(secrets_copy)
        live_config = AppConfig()
        live_config.database.db_path = str(db_copy)
        manager = SettingsManager(live_config, repository, store, SettingsHub(), no_environment(), {},
                                  config_path=str(config_path), file_raw=raw)
        await manager.load()
        await refresh_catalogue(repository)  # as the start does, right after the import
        report = {r["key"]: r["reason"] for r in manager.import_report()}
        old = old_effective(raw, str(config_path))
        problems = 0

        file_keys = [s.key for s in registry.SETTINGS if present(raw, s.key)]
        out(f"values: {len(file_keys)} set in config.yaml")
        for key in file_keys:
            if key in report:
                out(f"  {key}: skipped ({report[key]})")
                problems += 1
            elif old[key] == manager.effective_value(key):
                out(f"  {key}: same")
            else:
                out(f"  {key}: differs")
                problems += 1
        other = [s.key for s in registry.SETTINGS if s.key not in file_keys
                 and old[s.key] != manager.effective_value(s.key)]
        out(f"  other keys ({len(registry.SETTINGS) - len(file_keys)}, not in the file): "
            + (f"differ: {', '.join(other)}" if other else "same"))
        problems += len(other)

        file = read_settings(raw, move_secrets=True)
        hooks = file.document.lists.get("webhooks") or []
        if raw.get("webhooks") is not None:
            stored = {i.get("id"): i for i in manager.document.lists.get("webhooks") or []}
            urls = {hook: entry.get("url") for hook, entry in (file.secret_updates.get("webhooks") or {}).items()}
            same = sum(1 for item in hooks if stored.get(item["id"]) == item
                       and manager.stored_secret(f"webhook.{item['id']}.url") == urls.get(item["id"]))
            out(f"webhooks: {len(hooks)} in config.yaml, {len(stored)} stored, {same} same")
            problems += (len(hooks) - same) + abs(len(stored) - len(hooks))

        table = {s.id: s for s in await repository.get_services()}
        if file.services is not None:
            from penguwatch.db.services import same_refreshable
            differ = [s.id for s in file.services if s.id not in table or not same_refreshable(s, table[s.id])]
            out(f"services: {len(file.services)} in config.yaml, {len(file.services) - len(differ)} same"
                + (f"; differ: {', '.join(differ)}" if differ else ""))
            problems += len(differ)
        else:
            out(f"services: none in config.yaml; {len(table)} in the table")
        db = await repository.read_connection()
        cursor = await db.execute("SELECT COUNT(*) FROM services WHERE user_edited = 1")
        out(f"services edited by you (never refreshed by a release): {(await cursor.fetchone())[0]}")

        for key, why in report.items():
            if key not in file_keys:
                out(f"skipped: {key} ({why})")
                problems += 1
        for note in file.notes:
            out(f"note: {note}")
        out(f"store at revision {manager.revision}")
        if not live:
            versions = await repository.settings_versions()
            out(f"version 1: {versions[-1]['summary'] if versions else '(none)'}")
        out("result: " + ("every value came across unchanged" if not problems else f"{problems} problem(s)"))
        return 0 if not problems else 1
    finally:
        await repository.close()


def check_import(config: str, db: str, *, live: bool = False, out: Callable[[str], None] = print) -> int:
    """The dry run; returns the exit code. `config` and `db` are never written."""
    config_path, db_path = Path(config), Path(db)
    if not config_path.is_file():
        out(f"no config file at {config_path}")
        return 2
    if not db_path.is_file():
        out(f"no database at {db_path}")
        return 2
    try:
        _, raw = read_config_file(str(config_path), no_environment())
    except Exception as e:  # noqa: BLE001 - YAML or shape errors: named by type only (the text may quote a value)
        out(f"config.yaml cannot be read ({type(e).__name__})")
        return 2
    folder = Path(tempfile.mkdtemp(prefix="penguwatch-check-import-"))
    try:
        os.chmod(folder, stat.S_IRWXU)
        db_copy = folder / paths.DB_FILE
        _copy_db(db_path, db_copy)
        secrets_copy = paths.secrets_path(str(db_copy))
        secrets = paths.secrets_path(str(db_path))
        if secrets.is_file() and not secrets.is_symlink():
            shutil.copyfile(secrets, secrets_copy)
            os.chmod(secrets_copy, 0o600)
        return asyncio.run(_run(config_path, raw, db_copy, secrets_copy, live, out))
    except (sqlite3.Error, OSError) as e:
        out(f"the check could not run ({type(e).__name__})")
        return 2
    finally:
        shutil.rmtree(folder, ignore_errors=True)
