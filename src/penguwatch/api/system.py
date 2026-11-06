"""Health, dashboard statistics, and the telemetry endpoints (/api/system, /metrics)."""

import logging
import platform
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from penguwatch import clock
from penguwatch import __version__, activity
from penguwatch.api.deps import AppContainer, get_container, get_repo
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import DRIVERS
from penguwatch import telemetry
from penguwatch.observability import metrics, render_prometheus
from penguwatch.security import rate_limit, session_status

router = APIRouter(tags=["System"])
client_logger = logging.getLogger("penguwatch.client")

_MS = dict(ge=0, le=3_600_000)


class ClientTiming(BaseModel):
    """One API request the page found slow (>= 2 s), timed in the browser. The phases come from the browser's
    Resource Timing entry and server_ms from our Server-Timing header; any of them may be missing."""
    method: str = Field(pattern=r"^[A-Z]{3,7}$")
    path: str = Field(max_length=300, pattern=r"^/api/[^\s]*$")
    status: int = Field(ge=0, le=599)  # 0 = no response (network error or aborted)
    total_ms: float = Field(**_MS)
    server_ms: Optional[float] = Field(None, **_MS)
    page_ms: Optional[float] = Field(None, **_MS)  # in the page before the browser began the request (busy/frozen tab)
    wait_ms: Optional[float] = Field(None, **_MS)  # from fetch() to the request being sent: queueing, DNS, connect
    connect_ms: Optional[float] = Field(None, **_MS)
    ttfb_ms: Optional[float] = Field(None, **_MS)  # request sent -> first response byte
    download_ms: Optional[float] = Field(None, **_MS)
    visible: bool
    online: bool
    page_age_s: float = Field(ge=0, le=10_000_000)
    since_visible_s: Optional[float] = Field(None, ge=0, le=10_000_000)  # time since the tab last came to front
    connection: Optional[str] = Field(None, max_length=16, pattern=r"^[\w-]*$")


def _ms(label: str, value: Optional[float]) -> Optional[str]:
    return None if value is None else f"{label} {value:.0f} ms"


@router.post("/client-timing", summary="Report a slow request seen in the browser", status_code=204, dependencies=[Depends(rate_limit("client-timing", 30, 60))])
async def client_timing(report: ClientTiming, request: Request):
    """Logs a slow request as the browser saw it, next to what the server measured, and which device it was."""
    path = report.path.split("?", 1)[0]
    outcome = "network error" if report.status == 0 else str(report.status)
    parts = [p for p in (
        _ms("server", report.server_ms), _ms("page busy before sending", report.page_ms),
        _ms("waiting to send", report.wait_ms), _ms("connect", report.connect_ms),
        _ms("first byte", report.ttfb_ms), _ms("download", report.download_ms),
    ) if p]
    page = [f"page open {report.page_age_s:.0f} s", "visible" if report.visible else "hidden",
            "online" if report.online else "offline"]
    if report.since_visible_s is not None:
        page.append(f"visible again {report.since_visible_s:.0f} s ago")
    if report.connection:
        page.append(report.connection)
    agent = request.headers.get("user-agent", "")[:160].replace("\n", " ")
    host = request.client.host if request.client else "?"
    telemetry.SLOW_IN_BROWSER.add(1, {"outcome": "network_error" if report.status == 0 else "answered"})
    client_logger.warning(
        f"slow in the browser: {report.method} {path} -> {outcome}, {report.total_ms:.0f} ms in the browser"
        f" ({', '.join(parts) or 'no breakdown'}); {', '.join(page)}; client {host} {agent}"
    )
    return Response(status_code=204)
# Mounted at the app root (not under /api) so Prometheus can scrape the conventional path
metrics_router = APIRouter()


@router.get("/health", summary="Liveness check")
async def health():
    """Liveness: the process is up. Used by the container health check."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness check")
async def ready(repo: DatabaseRepository = Depends(get_repo)):
    """Readiness: the database answers."""
    try:
        db = await repo.read_connection()
        await (await db.execute("SELECT 1")).fetchone()
    except Exception:
        logging.getLogger("penguwatch.api.system").exception("readiness check: the database did not answer")
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ready"}


@router.get("/stats", summary="Dashboard statistics")
async def get_stats(repo: DatabaseRepository = Depends(get_repo)):
    stats = await repo.get_dashboard_stats()
    outbox = await repo.get_outbox()
    stats["pending_sync"] = len(outbox)
    return stats


def _rss_bytes() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _age_seconds(iso: str) -> float:
    if not iso:
        return -1.0
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (clock.utc_now() - when).total_seconds())
    except ValueError:
        return -1.0


async def _sync_facts(c: AppContainer) -> Dict[str, Any]:
    """The library driver's sync state (the System tab and the state gauges)."""
    sync = c.sync
    if sync is None:
        return {"driver": None, "label": None, "authenticated": False, "last_sync": None, "last_error": None,
                "review_mode": None}
    return {
        "driver": sync.library.name,
        "label": sync.library.label,
        "remote": sync.library.remote,
        "authenticated": sync.library.is_connected(),
        "last_sync": await sync.get_state("last_sync"),
        "last_error": await sync.get_state("last_error"),
        "review_mode": await sync.review_mode(),
    }


def _idle_facts(c: AppContainer) -> Dict[str, Any]:
    """Whether the background timer has backed off because nobody has used the app lately."""
    idle = activity.tracker.is_idle()
    library = c.sync.library if c.sync else None
    effective = (
        activity.pick_interval_minutes(idle, library.pull_interval_minutes, library.idle_pull_interval_minutes)
        if library else None
    )
    return {"idle": idle, "effective_interval_minutes": effective,
            "last_interaction_age_seconds": round(activity.tracker.age_seconds())}


@router.get("/session", summary="Sign-in state of this browser")
async def session(request: Request):
    """Whether this browser may sign out: `sign_out` when it signed in with the access token's cookie, and
    `sign_out_everywhere` when the app can also end every browser's sign-in. The Services & Sync tab reads it."""
    return session_status(request)


async def _setup_warnings(c: AppContainer) -> List[str]:
    """Things in the setup the person should fix, in words (shown on the System tab): a custom rating source that
    holds the id of a running driver's built-in one."""
    specs = [spec for driver in (c.drivers.by_name if c.drivers else {}) for spec in DRIVERS[driver].rating_sources
             if not spec.column]
    return await c.repository.rating_source_conflicts(specs)


@router.get("/system", summary="Everything the System tab shows")
async def system_status(c: AppContainer = Depends(get_container)):
    """Everything the System tab shows: build, database, sync history, API latency, upstream health."""
    snap = await c.repository.get_system_snapshot()
    facts = await _sync_facts(c)
    facts["age_seconds"] = _age_seconds(facts["last_sync"])
    now = time.time()
    return {
        "version": __version__,
        "python": platform.python_version(),
        "started_at": metrics.started_at,
        "uptime_seconds": round(now - metrics.started_at),
        "rss_bytes": _rss_bytes(),
        "database": {
            "bytes": snap["db_bytes"], "wal_bytes": snap["wal_bytes"], "shows": snap["shows"],
            "episodes": snap["episodes"], "shows_by_status": snap["shows_by_status"],
            "state_keys": snap["state_keys"],
        },
        "sync": {
            **facts,
            "outbox": snap["outbox"],
            "interval_minutes": c.sync.library.pull_interval_minutes if c.sync else None,
            **_idle_facts(c),
            "history": list(reversed(metrics.sync_runs)),
        },
        "http": {**metrics.http_totals(), "routes": metrics.route_summary()[:12]},
        "upstream": metrics.upstream_summary(),
        "logs": {k: metrics.log_counts.get(k, 0) for k in ("warning", "error")},
        "warnings": await _setup_warnings(c),
        "webhooks": {"hooks": c.webhooks.describe() if c.webhooks else [],
                     "recent": list(reversed(c.webhooks.recent)) if c.webhooks else []},
        "security": c.security.describe() if c.security else None,
        # Calendar apps can't send auth headers, so with a token set the feed URL carries a derived key
        "calendar_feed": (
            f"/api/calendar.ics?key={c.security.calendar_key}"
            if c.security and c.security.access_token else "/api/calendar.ics"
        ),
    }


@metrics_router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(c: AppContainer = Depends(get_container)):
    snap = await c.repository.get_system_snapshot()
    facts = await _sync_facts(c)
    driver = {"driver": facts["driver"] or ""}
    gauges: List[Tuple[str, str, Dict[str, Any], float]] = []
    for status, n in snap["shows_by_status"].items():
        gauges.append(("penguwatch_shows", "Titles in the library by status.", {"status": status}, n))
    gauges.append(("penguwatch_episodes", "Episode rows stored.", {}, snap["episodes"]))
    ob = snap["outbox"]
    pending = (ob["total"] or 0) - (ob["awaiting"] or 0)
    for label, value in (("pending", pending), ("awaiting_approval", ob["awaiting"] or 0), ("failing", ob["failing"] or 0)):
        gauges.append(("penguwatch_outbox_items", "Local changes waiting to reach the library.", {"state": label}, value))
    gauges.append(("penguwatch_outbox_oldest_age_seconds", "Age of the oldest queued change (-1 = none).", {},
                   _age_seconds(ob["oldest"].replace(" ", "T") + "Z") if ob["oldest"] else -1))
    gauges.append(("penguwatch_database_bytes", "SQLite file sizes.", {"file": "db"}, snap["db_bytes"]))
    gauges.append(("penguwatch_database_bytes", "SQLite file sizes.", {"file": "wal"}, snap["wal_bytes"]))
    gauges.append(("penguwatch_library_connected", "1 when the library driver's account is connected.", driver,
                   1 if facts["authenticated"] else 0))
    gauges.append(("penguwatch_library_last_sync_age_seconds", "Seconds since the last successful sync (-1 = never).",
                   driver, _age_seconds(facts["last_sync"])))
    gauges.append(("penguwatch_library_last_error", "1 when the last sync recorded an error.", driver,
                   1 if facts["last_error"] else 0))
    gauges.append(("penguwatch_review_mode", "1 when changes wait for approval.", {}, 1 if facts["review_mode"] else 0))
    gauges.append(("penguwatch_process_resident_memory_bytes", "Resident memory.", {}, _rss_bytes()))
    runs = list(metrics.sync_runs)
    for status in sorted({r["status"] for r in runs if r.get("status")}):
        gauges.append(("penguwatch_sync_runs", "Sync runs held in the history ring (last 50).", {"status": status},
                       sum(1 for r in runs if r["status"] == status)))
    if runs and runs[-1].get("duration_s") is not None:
        gauges.append(("penguwatch_sync_last_duration_seconds", "Duration of the most recent sync run.", {}, runs[-1]["duration_s"]))
    for hook in (c.webhooks.hooks if c.webhooks else []):
        gauges.append(("penguwatch_webhook_queue_depth", "Events waiting to be delivered, by webhook.",
                       {"webhook": hook.name}, hook.queue.qsize()))
    body = render_prometheus(metrics, gauges, __version__)
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")
