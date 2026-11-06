"""Characterization harness: snapshot every read-only endpoint against the sanitized seed library.

Not collected by pytest (no ``test_`` prefix). CLI (run from the repo root)::

    python -m tests.characterization record  <seed.db> <golden.json> [--src <dir>]
    python -m tests.characterization compare <seed.db> <golden.json> [--src <dir>]

``record`` runs the snapshot twice in separate interpreters, diffs the two runs and refuses to write the
golden file while any path differs that is not masked by ``VOLATILE`` / ``UNSTABLE_PATHS``.
``compare`` snapshots once and prints the first 20 differing paths (exit 1 on any difference).

Every snapshot runs in a *fresh* interpreter (``snapshot`` sub-command) because the date freeze has to be
installed before any ``penguwatch`` module is imported. The app is started with its real lifespan against a
temp copy of the seed DB, with no SIMKL/TMDB credentials, no notifier URL and no access token, so nothing
touches the network. ``--src`` points at the ``src`` directory to import ``penguwatch`` from (default:
``<repo>/src``); it is how a ``git archive`` extraction of another ref is snapshotted with this harness.

Non-JSON responses (CSV, ICS, SQLite backup): ``{"text": <body>}`` when the body is UTF-8 text of at most
20000 chars, otherwise ``{"sha256": ..., "length": ...}``. ``GET /`` stores only status and body length (the
HTML embeds the static-asset hash, which legitimately changes when the frontend changes).
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

VOLATILE = re.compile(
    r"^(uptime_seconds|started_at|request_id|rss_bytes|age_seconds|generated_at|p50_ms|p95_ms|max_ms|avg_ms|"
    r"duration_s|last_call_at|seconds_waited)$"
)

# Exact snapshot paths ("<route> :: <json path>") masked out because they differ between identical runs.
UNSTABLE_PATHS: set = set()

# Anything that calls upstream services or reports only process state.
DENYLIST = ("/api/simkl/", "/api/drivers/", "/api/catalog/", "/api/discover/", "/api/system", "/metrics",
            "/api/notifications/test")

FROZEN_SQL_NOW = "2026-10-10 12:00:00"

# Credentials the lifespan would read from app_state before the config/env fallback (the names before and after the
# driver layer moved them under driver.simkl.*).
SCRUBBED_STATE_KEYS = ("simkl_access_token", "simkl_refresh_token", "simkl_client_id", "tmdb_api_key",
                       "driver.simkl.access_token", "driver.simkl.refresh_token", "driver.simkl.client_id")
SCRUBBED_ENV = (
    "SIMKL_ACCESS_TOKEN", "SIMKL_CLIENT_ID", "TMDB_API_KEY", "TMDB_REGION", "PENGUWATCH_NOTIFY_URL",
    "PENGUWATCH_ACCESS_TOKEN", "PENGUWATCH_ALLOWED_HOSTS", "PENGUWATCH_ALLOWED_ORIGINS",
)


def normalize(obj):
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in sorted(obj.items()) if not VOLATILE.match(k)}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


# --------------------------------------------------------------------------------------------------
# snapshot (runs in its own interpreter)
# --------------------------------------------------------------------------------------------------

def _freeze_dates() -> None:
    import datetime as _dt

    frozen = _dt.datetime(2026, 10, 10, 12, 0, 0)

    class _Date(_dt.date):
        @classmethod
        def today(cls):
            return cls(frozen.year, frozen.month, frozen.day)

    class _DateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(frozen.year, frozen.month, frozen.day, 12, 0, 0, tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return cls(frozen.year, frozen.month, frozen.day, 12, 0, 0)

    _dt.date, _dt.datetime = _Date, _DateTime  # before importing penguwatch


def _freeze_sql_now() -> None:
    """Make SQLite's date('now'...) / datetime('now'...) return the frozen day on every new connection."""
    real_connect = sqlite3.connect
    mem = real_connect(":memory:", check_same_thread=False)  # plain connection with the real built-ins
    lock = threading.Lock()

    def make(name: str):
        def fn(*args):
            if args and args[0] == "now":
                args = (FROZEN_SQL_NOW,) + args[1:]
            with lock:
                return mem.execute(f"SELECT {name}({','.join('?' * len(args))})", args).fetchone()[0]
        return fn

    def connect(*a, **kw):
        conn = real_connect(*a, **kw)
        for name in ("date", "datetime"):
            conn.create_function(name, -1, make(name), deterministic=True)
        return conn

    sqlite3.connect = connect


def _is_listed(path: str) -> bool:
    return any(path == d.rstrip("/") or path.startswith(d if d.endswith("/") else d + "/") for d in DENYLIST)


def _walk_routes(routes, prefix: str = ""):
    """Yield (full_path, methods) for every route, descending into included routers.

    Recent FastAPI versions keep included routers as ``_IncludedRouter`` objects (with the include prefix in
    ``include_context``) instead of flattening them into ``app.routes``; older versions flatten them."""
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            ctx_prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from _walk_routes(original.routes, prefix + ctx_prefix)
        elif getattr(route, "methods", None):
            yield prefix + route.path, set(route.methods)


def _endpoint_paths(app) -> List[str]:
    out = []
    for p, methods in _walk_routes(app.routes):
        if "GET" in methods and p.startswith("/api") and "{" not in p and not _is_listed(p):
            out.append(p)
    return sorted(set(out))


def _has_route(app, path: str) -> bool:
    return any(p == path for p, _ in _walk_routes(app.routes))


def _body_entry(resp) -> Dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return {"json": normalize(resp.json())}
        except ValueError:
            pass
    raw = resp.content
    try:
        text = raw.decode("utf-8")
        if len(text) <= 20000:
            return {"text": text}
    except UnicodeDecodeError:
        pass
    return {"sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)}


def _mask(obj, route: str, path: str = "body"):
    if isinstance(obj, dict):
        return {k: _mask(v, route, f"{path}.{k}") for k, v in obj.items() if f"{route} :: {path}.{k}" not in UNSTABLE_PATHS}
    if isinstance(obj, list):
        return [_mask(v, route, f"{path}[{i}]") for i, v in enumerate(obj) if f"{route} :: {path}[{i}]" not in UNSTABLE_PATHS]
    return obj


async def _collect(app) -> Dict[str, Any]:
    from httpx import ASGITransport, AsyncClient

    result: Dict[str, Any] = {}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as client:
            paths = _endpoint_paths(app)
            show_ids: List[int] = []
            try:
                shows = (await client.get("/api/shows")).json()
                show_ids = sorted(s["id"] for s in shows)[:5]
            except Exception:
                pass
            has_detail = _has_route(app, "/api/shows/{show_id}")
            has_eps = _has_route(app, "/api/shows/{show_id}/episodes")
            for sid in show_ids:
                if has_detail:
                    paths.append(f"/api/shows/{sid}")
                if has_eps:
                    paths.append(f"/api/shows/{sid}/episodes")
            for path in paths:
                resp = await client.get(path)
                result[path] = {"status": resp.status_code, "body": _mask(_body_entry(resp), path)}
            resp = await client.get("/")
            result["/"] = {"status": resp.status_code, "body": {"length": len(resp.content)}}
    return result


def _block_network() -> None:
    """Refuse every real HTTP request (the in-process ASGI client is unaffected).

    Without credentials the app still tried SIMKL's public endpoints: a title with no episode list fetched it
    live during the snapshot, so the golden held whatever SIMKL answered that day (until SIMKL began to
    answer 412 on 2026-10-12)."""
    import httpx

    def refuse(self, request, *args, **kwargs):
        raise httpx.ConnectError(f"network disabled in the characterization harness: {request.url}", request=request)

    async def refuse_async(self, request, *args, **kwargs):
        refuse(self, request)

    httpx.HTTPTransport.handle_request = refuse
    httpx.AsyncHTTPTransport.handle_async_request = refuse_async


def snapshot(db_src: str, out_json: str) -> None:
    """Entry point of the snapshot sub-process."""
    _freeze_dates()
    _freeze_sql_now()
    _block_network()
    tmp = tempfile.mkdtemp(prefix="wt-char-")
    try:
        db_copy = os.path.join(tmp, "penguwatch.db")
        shutil.copyfile(db_src, db_copy)
        conn = sqlite3.connect(db_copy)
        marks = ",".join("?" * len(SCRUBBED_STATE_KEYS))
        try:
            removed = conn.execute(f"DELETE FROM app_state WHERE key IN ({marks})", SCRUBBED_STATE_KEYS).rowcount
        except sqlite3.OperationalError:
            removed = 0
        conn.commit()
        conn.close()
        print(f"[snapshot] scrubbed {removed} credential row(s) from the temp copy", file=sys.stderr)

        for name in SCRUBBED_ENV:
            os.environ[name] = ""
        os.environ["PENGUWATCH_DB_PATH"] = db_copy
        os.environ["PENGUWATCH_LOG_LEVEL"] = "warning"

        from penguwatch.main import app  # imported only now: env and date freeze are in place

        data = asyncio.run(_collect(app))
        Path(out_json).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------------------
# parent-side: run snapshots in subprocesses, diff
# --------------------------------------------------------------------------------------------------

def run_snapshot(db: str, src: Optional[str] = None) -> Dict[str, Any]:
    src_dir = Path(src) if src else REPO_ROOT / "src"
    out = tempfile.NamedTemporaryFile(prefix="wt-snap-", suffix=".json", delete=False)
    out.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "snapshot", str(db), out.name],
            cwd=str(src_dir.parent), env=env, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"snapshot subprocess failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
        return json.loads(Path(out.name).read_text(encoding="utf-8"))
    finally:
        try:
            os.unlink(out.name)
        except OSError:
            pass


_MISSING = "<missing>"


def _flatten(obj, prefix: str, out: Dict[str, Any]) -> None:
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}", out)
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj


def diff_snapshots(before: Dict[str, Any], after: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    diffs = []
    for route in sorted(set(before) | set(after)):
        a: Dict[str, Any] = {}
        b: Dict[str, Any] = {}
        if route in before:
            _flatten(before[route], "", a)
        if route in after:
            _flatten(after[route], "", b)
        for key in sorted(set(a) | set(b)):
            if a.get(key, _MISSING) != b.get(key, _MISSING):
                # "body.json.x[1]" style path, used verbatim in UNSTABLE_PATHS
                diffs.append((f"{route} :: {key.lstrip('.')}", a.get(key, _MISSING), b.get(key, _MISSING)))
    return diffs


def _short(value: Any, limit: int = 100) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def format_diffs(diffs: List[Tuple[str, Any, Any]], limit: int = 20) -> str:
    lines = [f"{len(diffs)} differing value(s); first {min(limit, len(diffs))}:"]
    for path, before, after in diffs[:limit]:
        lines.append(f"  {path}\n    before: {_short(before)}\n    after:  {_short(after)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# the driver layer: the pre-driver golden (.seed/golden.json) mapped to today's API shape
# --------------------------------------------------------------------------------------------------

LEGACY_ID_FIELDS = {"simkl_id": "simkl", "imdb_id": "imdb", "tmdb_id": "tmdb", "tvdb_id": "tvdb"}

# Differences the mapping cannot express, each with why it is expected
EXPLAINED = {
    "/api/export/backup.db :: body.sha256": "the backup is the database file, whose schema the driver layer changed",
    "/api/export/backup.db :: body.length": "the backup is the database file, whose schema the driver layer changed",
    "/ :: body.length": "the page markup became driver-neutral (labels and the account's controls come from "
                        "/api/drivers); no data differs",
    "/api/webhooks/events :: body.json.payload_version": "webhook payloads are version 2: a title's ids replaced its "
                                                          "simkl_id (the same change as the API's)",
    "/api/webhooks/events :: body.json.events.library.imported": "the event's description names the library driver, "
                                                                  "not a service",
    "/api/webhooks/events :: body.json.events.sync.failed": "the event's description names the library driver, not a "
                                                             "service",
}


def legacy_ids_to_ids(obj):
    """Every object that carried the id fields (titles, episodes, sequels, staged changes) carries `ids` instead:
    {"simkl_id": 1, "imdb_id": "tt2", "tmdb_id": None} -> {"ids": {"simkl": 1, "imdb": "tt2"}}. Keys stay sorted, as
    `normalize` leaves them."""
    if isinstance(obj, list):
        return [legacy_ids_to_ids(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {k: legacy_ids_to_ids(v) for k, v in obj.items() if k not in LEGACY_ID_FIELDS}
    if any(k in obj for k in LEGACY_ID_FIELDS):
        out["ids"] = {LEGACY_ID_FIELDS[k]: obj[k] for k in LEGACY_ID_FIELDS if obj.get(k) is not None}
    return {k: out[k] for k in sorted(out)}


def map_pre_driver_golden(golden: Dict[str, Any]) -> Dict[str, Any]:
    """The pre-driver snapshot in the driver layer's API shape: id fields -> ids (legacy_ids_to_ids). Routes keep
    their paths: every route the driver layer renamed (/api/simkl/*) is one the snapshot never covers (DENYLIST)."""
    mapped = {}
    for route, entry in golden.items():
        body = entry.get("body") or {}
        if "json" in body:
            entry = {**entry, "body": {**body, "json": legacy_ids_to_ids(body["json"])}}
        mapped[route] = entry
    return mapped


def compare_mapped(db: str, golden: str, src: Optional[str] = None,
                   record_to: Optional[str] = None) -> List[Tuple[str, Any, Any]]:
    """Snapshot `db`, diff it against the mapped pre-driver `golden`; returns the differences EXPLAINED does not
    cover (printing them). `record_to`: also write the snapshot there (the new golden)."""
    expected = map_pre_driver_golden(json.loads(Path(golden).read_text(encoding="utf-8")))
    actual = run_snapshot(db, src)
    if record_to:
        Path(record_to).write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    diffs = diff_snapshots(expected, actual)
    unexplained = [d for d in diffs if d[0] not in EXPLAINED]
    for path, _, _ in diffs:
        if path in EXPLAINED:
            print(f"expected difference: {path}: {EXPLAINED[path]}", file=sys.stderr)
    if unexplained:
        print(format_diffs(unexplained), file=sys.stderr)
    return unexplained


def compare(db: str, golden: str, src: Optional[str] = None) -> List[Tuple[str, Any, Any]]:
    """Snapshot ``db`` in a fresh interpreter and diff against ``golden``. Returns the differences."""
    expected = json.loads(Path(golden).read_text(encoding="utf-8"))
    actual = run_snapshot(db, src)
    diffs = diff_snapshots(expected, actual)
    if diffs:
        print(format_diffs(diffs), file=sys.stderr)
    return diffs


def record(db: str, golden: str, src: Optional[str] = None) -> bool:
    first = run_snapshot(db, src)
    second = run_snapshot(db, src)
    unstable = diff_snapshots(first, second)
    if unstable:
        print("Unstable between two identical runs; add these paths to VOLATILE or UNSTABLE_PATHS:", file=sys.stderr)
        for path, a, b in unstable:
            print(f"  {path!r}\n    run1: {_short(a)}\n    run2: {_short(b)}", file=sys.stderr)
        return False
    Path(golden).write_text(json.dumps(first, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {len(first)} endpoints -> {golden}")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="tests.characterization")
    ap.add_argument("command", choices=["record", "compare", "snapshot", "compare-mapped"])
    ap.add_argument("db")
    ap.add_argument("out", help="golden.json (record/compare) or output json (snapshot)")
    ap.add_argument("--src", default=None, help="src directory to import penguwatch from (default <repo>/src)")
    args = ap.parse_args(argv)
    if args.command == "snapshot":
        snapshot(args.db, args.out)
        return 0
    if args.command == "record":
        return 0 if record(args.db, args.out, args.src) else 1
    if args.command == "compare-mapped":
        return 1 if compare_mapped(args.db, args.out, args.src) else 0
    return 1 if compare(args.db, args.out, args.src) else 0


if __name__ == "__main__":
    sys.exit(main())
