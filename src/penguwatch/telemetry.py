"""OpenTelemetry for PenguWatch: traces, metrics and logs sent over OTLP/HTTP to an OpenTelemetry collector,
next to the in-process numbers of observability.py that the System tab and /metrics keep showing.

Nothing is exported unless an OTLP endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT, or OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT
for one signal); OTEL_SDK_DISABLED=true turns export off. OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES have the last
word over the resource defaults here. Without an endpoint every instrument and span below is the API's no-op.

What goes out:
  * traces: FastAPI's request spans (with its dependency, endpoint and serialization spans), one CLIENT span per upstream
    call attempt (a driver's service), and a span per sync run (`sync.run`), pushed batch (`sync.push`), token renewal
    (the driver's) and daily notification check (`notify.daily_check`); the background ones are root spans;
  * metrics, every METRIC_EXPORT_INTERVAL_MS: FastAPI's http.server.* metrics, upstream calls, sync runs, worker steps,
    changes pushed and pulled, token renewals, notifications, warnings and errors logged, event-loop lag, process
    memory and CPU, and gauges of the app state (library, outbox, the library driver's login, database) that `sample_loop` refreshes
    every SAMPLE_SECONDS;
  * logs: INFO and above from the app's loggers, credentials masked (observability.redact), tied to the span they were
    logged in and carrying the request id.

Content rule: identifiers, outcomes and measurements only. No titles, search text, tokens or the calendar key. Span
attributes come from a closed set (`_ATTRS`); metric labels are fixed strings or normalised upstream path templates; the
query string FastAPI records on request spans keeps its parameter names but not their values (`QueryValuesRedactor`);
requests that carry a credential in their body (/login) are not traced. Debug lines (which may name a title) are never
exported.
"""

import asyncio
import contextlib
import contextvars
import functools
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, MutableMapping, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

from opentelemetry import _logs, metrics, trace
from opentelemetry import context as otel_context
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, StatusCode

from penguwatch import __version__
from penguwatch.observability import metrics as local_metrics
from penguwatch.observability import normalize_upstream_path, redact, request_id_var

log = logging.getLogger("penguwatch.telemetry")

NAME = "penguwatch"
SERVICE_NAME = "penguwatch"
METRIC_EXPORT_INTERVAL_MS = 30000
SHUTDOWN_TIMEOUT_MS = 2000  # per provider (3): with the outbox flush (up to 10 s) still inside the 20 s stop_grace_period
SAMPLE_SECONDS = 15
# The loggers whose records are exported. Not the root: libraries' own lines (the OTLP exporter's retries above all)
# would loop back into the exporter.
LOGGERS = ("penguwatch", "homeguard", "uvicorn.error", "fastapi")
EXPORT_MIN_LEVEL = logging.INFO  # debug lines can name a title
_UVICORN_MIN_LEVEL = logging.WARNING  # uvicorn's INFO lines name the client's address and port

tracer = trace.get_tracer(NAME, __version__)
meter = metrics.get_meter(NAME, __version__)

# --- instruments -------------------------------------------------------------------------------------------------
# Every histogram in seconds names its buckets: the SDK's defaults (0, 5, 10, 25, ...) are meant for milliseconds.

UPSTREAM_BUCKETS = [0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0]
SYNC_BUCKETS = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0]
LAG_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]

UPSTREAM_REQUESTS = meter.create_counter(
    "penguwatch.upstream.requests", unit="{request}",
    description="Calls to a driver's service by service, endpoint template and HTTP status (0: network error)")
UPSTREAM_DURATION = meter.create_histogram(
    "penguwatch.upstream.request.duration", unit="s",
    description="Time one call to a driver's service took, by service and endpoint template",
    explicit_bucket_boundaries_advisory=UPSTREAM_BUCKETS)
SYNC_RUNS = meter.create_counter(
    "penguwatch.sync.runs", unit="{run}", description="Sync runs (push + pull + refresh) by trigger, status and mode")
SYNC_DURATION = meter.create_histogram(
    "penguwatch.sync.duration", unit="s", description="Time one sync run took, by trigger and status",
    explicit_bucket_boundaries_advisory=SYNC_BUCKETS)
SYNC_CHANGES = meter.create_counter(
    "penguwatch.sync.changes", unit="{change}",
    description="Changes sent to the library driver (pushed) and titles updated from it (pulled), by driver")
WORKER_STEPS = meter.create_counter(
    "penguwatch.worker.steps", unit="{step}",
    description="Background sync worker passes: pulled, flushed (push only), paused (reconnect needed) or error")
TOKEN_REFRESHES = meter.create_counter(
    "penguwatch.login.refreshes", unit="{refresh}", description="Login (token) renewals by driver and outcome")
NOTIFICATIONS = meter.create_counter(
    "penguwatch.notifications", unit="{notification}", description="Notifications by outcome (sent or failed)")
WEBHOOK_DELIVERIES = meter.create_counter(
    "penguwatch.webhook.deliveries", unit="{delivery}", description="Event webhook deliveries by outcome (delivered, failed or dropped)")
SLOW_IN_BROWSER = meter.create_counter(
    "penguwatch.client.slow_requests", unit="{request}",
    description="API requests the page found slow (>= 2 s in the browser), by outcome")
# The Settings tab (penguwatch.settings.manager); attributes are fixed words, never a key's value or a secret
SETTINGS_SAVES = meter.create_counter(
    "penguwatch.settings.saves", unit="{save}",
    description="Settings saves by source (save, restore, yaml_import) and outcome (saved, invalid, conflict, failed)")
SETTINGS_IMPORTS = meter.create_counter(
    "penguwatch.settings.imports", unit="{import}",
    description="Settings imports by kind (config_yaml: the one-time import at the first start; yaml: Settings > "
                "Import drafts) and outcome")
SETTINGS_LIVE_FAILURES = meter.create_counter(
    "penguwatch.settings.live_failures", unit="{failure}",
    description="Settings appliers that raised after a save (the change applies at the next start), by applier")
LOOP_LAG = meter.create_histogram(
    "penguwatch.event_loop.lag", unit="s",
    description="How late a 1 s timer fired: time the event loop was blocked or busy",
    explicit_bucket_boundaries_advisory=LAG_BUCKETS)

# --- gauges of the app state ---------------------------------------------------------------------------------------
# Callbacks run on the SDK's export thread, where the database (aiosqlite, bound to the event loop) can't be read:
# sample_loop reads it on the loop every SAMPLE_SECONDS and leaves the values here.

_state: Dict[str, Any] = {"values": {}, "worker_beat": None}

_GAUGES: Dict[str, Tuple[str, str, str]] = {
    # key: (instrument name, unit, description)
    "titles": ("penguwatch.library.titles", "{title}", "Titles in the library by status"),
    "episodes": ("penguwatch.library.episodes", "{episode}", "Episode rows stored"),
    "outbox": ("penguwatch.outbox.items", "{change}",
               "Local changes waiting to reach the library, by state (pending, awaiting_approval, failing)"),
    "outbox_age": ("penguwatch.outbox.oldest.age", "s", "Age of the oldest queued change (absent: queue empty)"),
    "db": ("penguwatch.database.size", "By", "SQLite file sizes (db, wal)"),
    "connected": ("penguwatch.library.connected", "{state}", "1 when the library driver's account is connected"),
    "reconnect": ("penguwatch.library.reconnect_needed", "{state}",
                  "1 while the library login needs a person to reconnect it (background sync is paused)"),
    "last_sync_age": ("penguwatch.library.last_sync.age", "s",
                      "Seconds since the last successful sync (absent: never)"),
    "last_error": ("penguwatch.library.last_error", "{state}", "1 when the last sync recorded an error"),
    "review": ("penguwatch.sync.review_mode", "{state}", "1 when changes wait for approval before going to the library"),
    "idle": ("penguwatch.sync.idle", "{state}", "1 while the worker pulls at the idle interval (nobody using the app)"),
}


def _from_state(key: str) -> Callable[[CallbackOptions], List[Observation]]:
    def observe(options: CallbackOptions) -> List[Observation]:
        return [Observation(value, attrs) for value, attrs in _state["values"].get(key, ())]
    return observe


for _key, (_name, _unit, _description) in _GAUGES.items():
    meter.create_observable_gauge(_name, [_from_state(_key)], unit=_unit, description=_description)


def _worker_age(options: CallbackOptions) -> List[Observation]:
    beat = _state["worker_beat"]
    return [Observation(max(0.0, time.monotonic() - beat))] if beat is not None else []


def _rss(options: CallbackOptions) -> List[Observation]:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return [Observation(int(line.split()[1]) * 1024)]
    except OSError:
        pass
    return []


def _cpu(options: CallbackOptions) -> List[Observation]:
    return [Observation(time.process_time())]


def _log_records(options: CallbackOptions) -> List[Observation]:
    counts = local_metrics.log_counts  # counted by observability.ContextFilter on the stdout handler
    return [Observation(counts.get(level, 0), {"level": level}) for level in ("warning", "error", "critical")]


meter.create_observable_gauge(
    "penguwatch.sync.worker.age", [_worker_age], unit="s",
    description="Seconds since the background sync worker last showed life: woke up, or got an upstream answer during a "
                "sync (it waits at most 2 minutes between passes; much more = stuck)")
meter.create_observable_gauge(
    "penguwatch.process.memory", [_rss], unit="By", description="Resident memory of the process")
meter.create_observable_gauge(
    "penguwatch.process.start_time", [lambda options: [Observation(local_metrics.started_at)]], unit="s",
    description="Unix time the process started (a change = a restart)")
meter.create_observable_counter(
    "penguwatch.process.cpu.time", [_cpu], unit="s", description="CPU time used by the process")
meter.create_observable_counter(
    "penguwatch.log.records", [_log_records], unit="{record}", description="Warnings and errors logged, by level")


async def collect_state(container: Any) -> Dict[str, List[Tuple[float, Dict[str, str]]]]:
    """The gauge values, read from the database and the app state: the same facts /metrics shows."""
    from penguwatch import activity
    from penguwatch.api.system import _age_seconds, _sync_facts

    repo = container.repository
    snap = await repo.get_system_snapshot()
    facts = await _sync_facts(container)
    driver = {"driver": facts["driver"] or ""}
    reconnect = bool(container.sync and await container.sync.library.needs_reconnect())
    ob = snap["outbox"]
    values: Dict[str, List[Tuple[float, Dict[str, str]]]] = {
        "titles": [(n, {"status": status}) for status, n in snap["shows_by_status"].items()],
        "episodes": [(snap["episodes"], {})],
        "outbox": [((ob["total"] or 0) - (ob["awaiting"] or 0), {"state": "pending"}),
                   (ob["awaiting"] or 0, {"state": "awaiting_approval"}),
                   (ob["failing"] or 0, {"state": "failing"})],
        "db": [(snap["db_bytes"], {"file": "db"}), (snap["wal_bytes"], {"file": "wal"})],
        "connected": [(1 if facts["authenticated"] else 0, driver)],
        "reconnect": [(1 if reconnect else 0, driver)],
        "last_error": [(1 if facts["last_error"] else 0, driver)],
        "review": [(1 if facts["review_mode"] else 0, {})],
        "idle": [(1 if activity.tracker.is_idle() else 0, {})],
    }
    if ob["oldest"]:
        values["outbox_age"] = [(_age_seconds(ob["oldest"].replace(" ", "T") + "Z"), {})]
    age = _age_seconds(facts["last_sync"])
    if age >= 0:
        values["last_sync_age"] = [(age, driver)]
    return values


async def sample_loop(container: Any, interval: float = SAMPLE_SECONDS, tick: float = 1.0) -> None:
    """Measures event-loop lag every `tick` and refreshes the state gauges every `interval` seconds."""
    loop = asyncio.get_running_loop()
    last_sample = None
    failing = False
    while True:
        started = loop.time()
        await asyncio.sleep(tick)
        now = loop.time()
        LOOP_LAG.record(max(0.0, now - started - tick))
        if last_sample is not None and now - last_sample < interval:
            continue
        last_sample = now
        try:
            _state["values"] = await collect_state(container)
            if failing:
                log.info("Telemetry state sampling works again")
            failing = False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep the last values; say so once, not every 15 s
            if not failing:
                log.warning(f"Telemetry state sampling failed: {type(e).__name__}: {e}")
            failing = True


# True inside the background sync worker's task (and tasks it starts): its upstream calls are signs of life
in_worker: contextvars.ContextVar[bool] = contextvars.ContextVar("penguwatch_in_worker", default=False)


def settings_live_failure(applier: str) -> None:
    """The settings hub's on_failure: one applier raised after a save (its name is a fixed word such as clock.zone)."""
    SETTINGS_LIVE_FAILURES.add(1, {"applier": applier})


def worker_beat(step: Optional[str] = None) -> None:
    """The sync worker is alive (and, with `step`, finished a pass of that kind)."""
    _state["worker_beat"] = time.monotonic()
    if step:
        WORKER_STEPS.add(1, {"step": step})


# --- hooks for the existing in-process telemetry ------------------------------------------------------------------

def observe_upstream(service: str, path: str, status: int, seconds: float) -> None:
    """A driver client's on_request: the in-process numbers plus the OTel metrics."""
    local_metrics.observe_upstream(service, path, status, seconds)
    if in_worker.get():
        worker_beat()  # a long sync makes progress call by call; only a worker that stops calling looks stuck
    endpoint = normalize_upstream_path(path)
    UPSTREAM_REQUESTS.add(1, {"service": service, "endpoint": endpoint, "status": str(status)})
    UPSTREAM_DURATION.record(seconds, {"service": service, "endpoint": endpoint})


def record_sync_run(trigger: str, started_at: float, result: Dict[str, Any]) -> None:
    """SyncEngine.on_run: the in-process history plus the OTel metrics, and the outcome on the sync span."""
    local_metrics.record_sync_run(trigger, started_at, result)
    status = str(result.get("status") or "unknown")
    mode = str(result.get("mode") or "none")
    SYNC_RUNS.add(1, {"trigger": trigger, "status": status, "mode": mode})
    if result.get("duration_s") is not None:
        SYNC_DURATION.record(float(result["duration_s"]), {"trigger": trigger, "status": status})
    if result.get("updated"):
        SYNC_CHANGES.add(int(result["updated"]), {"direction": "pulled"})
    annotate(status=status, mode=mode, pushed=_int(result.get("pushed")), pulled=_int(result.get("updated")))
    if status == "error":
        sp = trace.get_current_span()
        if sp.is_recording():
            sp.set_status(StatusCode.ERROR, "sync failed")


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --- spans --------------------------------------------------------------------------------------------------------

# The only attributes the app's own spans get: a closed set, so a title or a token cannot be passed in by mistake
_ATTRS = {
    "trigger": "penguwatch.sync.trigger",
    "mode": "penguwatch.sync.mode",
    "status": "penguwatch.sync.status",
    "pushed": "penguwatch.sync.pushed",
    "pulled": "penguwatch.sync.pulled",
    "op": "penguwatch.push.op",
    "rows": "penguwatch.push.rows",
    "outcome": "penguwatch.outcome",
    "sent": "penguwatch.notify.sent",
    "request_id": "penguwatch.request_id",
    "attempt": "penguwatch.upstream.attempt",
}


def _attributes(attrs: Mapping[str, Any]) -> Dict[str, Any]:
    return {_ATTRS[k]: v for k, v in attrs.items() if v is not None}


def annotate(**attrs: Any) -> None:
    """Adds attributes (keys of `_ATTRS` only) to the current span."""
    sp = trace.get_current_span()
    if sp.is_recording():
        sp.set_attributes(_attributes(attrs))


def _fail(sp: trace.Span, e: BaseException) -> None:
    """Marks the span failed by the exception's type alone: its message can quote an upstream answer or a title."""
    kind = type(e).__qualname__
    sp.set_attribute("error.type", kind)
    sp.set_status(StatusCode.ERROR, kind)


@contextlib.contextmanager
def span(name: str, kind: SpanKind = SpanKind.INTERNAL, **attrs: Any) -> Iterator[trace.Span]:
    """An app span with attributes from `_ATTRS`; an exception fails it (type only) and propagates."""
    with tracer.start_as_current_span(name, kind=kind, attributes=_attributes(attrs), record_exception=False,
                                      set_status_on_exception=False) as sp:
        try:
            yield sp
        except asyncio.CancelledError:
            sp.set_attribute(_ATTRS["outcome"], "cancelled")
            raise
        except Exception as e:
            _fail(sp, e)
            raise


def traced(name: str, attrs: Optional[Callable[..., Mapping[str, Any]]] = None, outcome: bool = False):
    """Decorates an async function with `span(name)`. `attrs(*args, **kwargs)` gives its attributes (keys of
    `_ATTRS`); with `outcome`, a str result becomes the span's penguwatch.outcome."""
    def wrap(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            with span(name, **(attrs(*args, **kwargs) if attrs else {})) as sp:
                result = await fn(*args, **kwargs)
                if outcome and isinstance(result, str):
                    sp.set_attribute(_ATTRS["outcome"], result)
                return result
        return run
    return wrap


@contextlib.contextmanager
def upstream_call(service: str, host: str, method: str, path: str, attempt: int) -> Iterator[Callable[[int], None]]:
    """A CLIENT span for one HTTP attempt to a driver's service (`service`: what to call it in the span name, `host`:
    its address). Yields `done(status)` to call with the response's status code."""
    template = normalize_upstream_path(path)
    attributes = {"http.request.method": method, "url.template": template, "server.address": host,
                  _ATTRS["attempt"]: attempt}
    with tracer.start_as_current_span(f"{service} {method} {template}", kind=SpanKind.CLIENT, attributes=attributes,
                                      record_exception=False, set_status_on_exception=False) as sp:
        def done(status: int) -> None:
            sp.set_attribute("http.response.status_code", status)
            if status >= 400:
                sp.set_attribute("error.type", str(status))
                sp.set_status(StatusCode.ERROR)
        try:
            yield done
        except Exception as e:
            _fail(sp, e)
            raise


# --- request spans: what FastAPI's telemetry records and skips -----------------------------------------------------

# Static assets, the health checks (polled by uptime monitors) and the Prometheus scrape are high-volume and say
# nothing about a fault; /login carries the access token in its body.
_EXCLUDED_PREFIXES = ("/static/",)
_EXCLUDED_PATHS = frozenset({"/api/health", "/api/health/ready", "/metrics", "/login", "/favicon.ico",
                             "/sw.js", "/manifest.webmanifest"})


def exclude_from_telemetry(scope: MutableMapping[str, Any]) -> bool:
    path = scope.get("path") or ""
    return path in _EXCLUDED_PATHS or path.startswith(_EXCLUDED_PREFIXES)


def fastapi_telemetry() -> Dict[str, Any]:
    """The `telemetry=` argument for FastAPI(). FastAPI's own exporter setup is off: setup_telemetry installs the
    providers (before the app starts) and FastAPI's spans and metrics use them; with it on, FastAPI would add a second
    exporter to each provider and every span would go out twice."""
    return {"exclude": exclude_from_telemetry, "auto_configure": False}


def names_only(query: str) -> str:
    """'q=breaking+bad&page=2' -> 'q=REDACTED&page=REDACTED'."""
    return urlencode([(k, "REDACTED") for k, _ in parse_qsl(query, keep_blank_values=True)])


class QueryValuesRedactor:
    """A span processor that keeps the parameter names of FastAPI's `url.query` attribute and drops the values: a
    title search, the calendar feed's key. Runs at span start, before anything is exported."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        query = (span.attributes or {}).get("url.query")
        if query:
            span.set_attribute("url.query", names_only(str(query)))

    def on_end(self, span: Any) -> None:
        pass

    def _on_ending(self, span: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# --- logs ---------------------------------------------------------------------------------------------------------

_SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG, logging.INFO: SeverityNumber.INFO, logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR, logging.CRITICAL: SeverityNumber.FATAL,
}


class OtelLogHandler(logging.Handler):
    """Sends log records to an OpenTelemetry logger provider, masked like the stdout log (this handler runs before
    the root handler's ContextFilter has masked the record), with the request id, tied to the current span."""

    def __init__(self, provider: Any, level: int = EXPORT_MIN_LEVEL):
        super().__init__(level)
        self._logger = provider.get_logger(NAME, __version__)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("uvicorn") and record.levelno < _UVICORN_MIN_LEVEL:
            return False
        return super().filter(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            attributes: Dict[str, Any] = {"logger.name": record.name}
            rid = request_id_var.get()
            if rid != "-":
                attributes["penguwatch.request_id"] = rid
            if record.exc_info and record.exc_info[0] is not None:
                attributes["exception.type"] = record.exc_info[0].__name__
                attributes["exception.stacktrace"] = redact(logging.Formatter().formatException(record.exc_info))
            self._logger.emit(LogRecord(
                timestamp=int(record.created * 1e9),
                observed_timestamp=time.time_ns(),
                context=otel_context.get_current(),
                severity_text=record.levelname,
                severity_number=_SEVERITY.get(record.levelno, SeverityNumber.UNSPECIFIED),
                body=redact(record.getMessage()),
                attributes=attributes,
            ))
        except Exception:  # a telemetry problem must never break logging
            self.handleError(record)


# --- setup --------------------------------------------------------------------------------------------------------

@dataclass
class Telemetry:
    """The providers this process exports with (None: that signal is not exported)."""

    tracer_provider: Any = None
    meter_provider: Any = None
    logger_provider: Any = None
    log_handler: Optional[logging.Handler] = None
    signals: List[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.signals)

    def shutdown(self) -> None:
        """Flushes and stops every exporter, each bounded by SHUTDOWN_TIMEOUT_MS, so a stop never hangs on an
        unreachable collector. Safe to call twice."""
        if self.log_handler is not None:
            for name in LOGGERS:
                logging.getLogger(name).removeHandler(self.log_handler)
            self.log_handler = None
        providers = [p for p in (self.tracer_provider, self.meter_provider, self.logger_provider) if p is not None]
        self.tracer_provider = self.meter_provider = self.logger_provider = None
        for provider in providers:
            try:
                provider.force_flush(SHUTDOWN_TIMEOUT_MS)
                provider.shutdown()
            except Exception as e:  # the process is stopping: say so and go on
                log.warning(f"Telemetry shutdown: {type(provider).__name__} failed: {type(e).__name__}")


def _endpoint(signal: str, env: Mapping[str, str]) -> Optional[str]:
    return env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None


def _signal_url(signal: str, env: Mapping[str, str]) -> str:
    """The URL an exporter posts to: a per-signal endpoint as it is, the general one with /v1/<signal> appended."""
    specific = env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific
    return f"{env['OTEL_EXPORTER_OTLP_ENDPOINT'].rstrip('/')}/v1/{signal}"


_installed: List[Telemetry] = []  # the process's global Telemetry (OpenTelemetry allows one set per process)


def setup_telemetry(environ: Optional[Mapping[str, str]] = None, *, set_global: bool = True) -> Telemetry:
    """Builds the exporting providers for every signal with an endpoint and, with `set_global`, installs them as the
    process's providers (OpenTelemetry allows that once per process; tests pass False). Returns a disabled Telemetry
    when nothing is to be exported, or when the SDK is not installed. With `set_global`, a second call returns the
    first one's Telemetry. FastAPI's request telemetry looks the global providers up per request, so calling this at
    the start of the lifespan (before any request) is early enough."""
    if set_global and _installed:
        # A second start in the same process (the app's lifespan run again, as in tests): the providers are set
        # once; after a shutdown they stay shut down
        return _installed[0]
    tel = _build(os.environ if environ is None else environ, set_global)
    if set_global:
        _installed.append(tel)
    return tel


def _build(env: Mapping[str, str], set_global: bool) -> Telemetry:
    if (env.get("OTEL_SDK_DISABLED") or "").strip().lower() == "true":
        return Telemetry()
    signals = [s for s in ("traces", "metrics", "logs") if _endpoint(s, env)]
    if not signals:
        return Telemetry()
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("An OTLP endpoint is set but the OpenTelemetry SDK is not installed; nothing is exported")
        return Telemetry()

    # Resource.create adds OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME from the process environment; without a
    # service name from there the SDK says "unknown_service", and penguwatch is the better default
    resource = Resource.create({"service.version": __version__})
    if str(resource.attributes.get("service.name", "")).startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": SERVICE_NAME}))
    tel = Telemetry(signals=signals)
    # shutdown_on_exit=False: Telemetry.shutdown, from the lifespan, is the one bounded shutdown
    if "traces" in signals:
        tel.tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        tel.tracer_provider.add_span_processor(QueryValuesRedactor())
        tel.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_signal_url("traces", env))))
    if "metrics" in signals:
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=_signal_url("metrics", env)),
                                               export_interval_millis=METRIC_EXPORT_INTERVAL_MS)
        tel.meter_provider = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
    if "logs" in signals:
        tel.logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        tel.logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=_signal_url("logs", env))))
        tel.log_handler = OtelLogHandler(tel.logger_provider)
        for name in LOGGERS:
            logging.getLogger(name).addHandler(tel.log_handler)
    if set_global:
        if tel.tracer_provider is not None:
            trace.set_tracer_provider(tel.tracer_provider)
        if tel.meter_provider is not None:
            metrics.set_meter_provider(tel.meter_provider)
        if tel.logger_provider is not None:
            _logs.set_logger_provider(tel.logger_provider)
    log.info(f"Exporting {', '.join(signals)} over OTLP to {_endpoint(signals[0], env)} "
             f"as {resource.attributes.get('service.name')}")
    return tel
