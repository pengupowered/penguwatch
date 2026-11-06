"""Telemetry for PenguWatch: request metrics, upstream-call metrics, sync-run history,
correlation IDs, structured logs and Prometheus exposition.

Everything here is in-process (no prometheus_client); it is what the System tab and /metrics show,
and it resets when the container restarts. Export to an OpenTelemetry collector is telemetry.py's job;
the middleware here only puts the request id on FastAPI's request span.
"""

import contextvars
import json
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from opentelemetry import trace

from penguwatch.config import EnvOverrides

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

HTTP_BUCKETS: Tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ID_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")


def normalize_upstream_path(path: str) -> str:
    """'/tv/12345/seasons' -> '/tv/{id}/seasons' so label cardinality stays small."""
    path = path.split("?", 1)[0]
    if path.startswith("http"):
        path = "/" + path.split("/", 3)[-1]
    return _ID_SEGMENT_RE.sub("/{id}", path)[:80]


def _percentile(sorted_values: List[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


class Metrics:
    """Process-wide metric registry (single event loop, so plain dicts are safe)."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.in_flight = 0
        # (method, route, status_class) -> count
        self.http_count: Dict[Tuple[str, str, str], int] = defaultdict(int)
        # route -> [bucket counts..., +Inf]
        self.http_buckets: Dict[str, List[int]] = defaultdict(lambda: [0] * (len(HTTP_BUCKETS) + 1))
        self.http_sum: Dict[str, float] = defaultdict(float)
        self.http_samples: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=300))
        self.http_errors: Dict[str, int] = defaultdict(int)  # route -> 5xx count
        # (service, endpoint, status) -> count ; (service, endpoint) -> seconds
        self.upstream_count: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self.upstream_seconds: Dict[Tuple[str, str], float] = defaultdict(float)
        self.upstream_last: Dict[str, Dict[str, Any]] = {}
        self.log_counts: Dict[str, int] = defaultdict(int)
        self.sync_runs: Deque[Dict[str, Any]] = deque(maxlen=50)
        # (webhook name, outcome) -> count
        self.webhook_count: Dict[Tuple[str, str], int] = defaultdict(int)

    # -- recording ---------------------------------------------------------

    def observe_http(self, method: str, route: str, status: int, seconds: float) -> None:
        self.http_count[(method, route, f"{status // 100}xx")] += 1
        buckets = self.http_buckets[route]
        for i, bound in enumerate(HTTP_BUCKETS):
            if seconds <= bound:
                buckets[i] += 1
        buckets[-1] += 1  # +Inf (== _count)
        self.http_sum[route] += seconds
        self.http_samples[route].append(seconds)
        if status >= 500:
            self.http_errors[route] += 1

    def observe_webhook(self, name: str, outcome: str) -> None:
        self.webhook_count[(name, outcome)] += 1

    def observe_upstream(self, service: str, endpoint: str, status: int, seconds: float) -> None:
        endpoint = normalize_upstream_path(endpoint)
        self.upstream_count[(service, endpoint, str(status))] += 1
        self.upstream_seconds[(service, endpoint)] += seconds
        self.upstream_last[service] = {
            "endpoint": endpoint, "status": status, "ms": round(seconds * 1000, 1), "at": time.time(),
        }

    def record_sync_run(self, trigger: str, started_at: float, result: Dict[str, Any]) -> None:
        self.sync_runs.append({
            "started_at": started_at,
            "duration_s": result.get("duration_s"),
            "trigger": trigger,
            "status": result.get("status"),
            "mode": result.get("mode"),
            "pushed": result.get("pushed"),
            "updated": result.get("updated"),
            "enriched": result.get("enriched"),
            "error": (result.get("error") or result.get("reason") or None),
        })

    # -- reading -----------------------------------------------------------

    def route_summary(self) -> List[Dict[str, Any]]:
        rows = []
        for route, samples in self.http_samples.items():
            data = sorted(samples)
            total = self.http_buckets[route][-1]
            rows.append({
                "route": route,
                "count": total,
                "errors": self.http_errors.get(route, 0),
                "p50_ms": round(_percentile(data, 0.50) * 1000, 1),
                "p95_ms": round(_percentile(data, 0.95) * 1000, 1),
                "max_ms": round((data[-1] if data else 0.0) * 1000, 1),
                "avg_ms": round(self.http_sum[route] / total * 1000, 1) if total else 0.0,
            })
        rows.sort(key=lambda r: r["p95_ms"], reverse=True)
        return rows

    def upstream_summary(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for (service, endpoint, status), n in self.upstream_count.items():
            s = out.setdefault(service, {"requests": 0, "errors": 0, "seconds": 0.0, "by_status": {}, "last": None})
            s["requests"] += n
            s["by_status"][status] = s["by_status"].get(status, 0) + n
            if not status.startswith("2"):
                s["errors"] += n
        for (service, _endpoint), secs in self.upstream_seconds.items():
            out[service]["seconds"] = round(out[service]["seconds"] + secs, 2)
        for service, last in self.upstream_last.items():
            if service in out:
                out[service]["last"] = last
        return out

    def http_totals(self) -> Dict[str, Any]:
        total = sum(self.http_count.values())
        errors = sum(n for (_m, _r, c), n in self.http_count.items() if c == "5xx")
        client_errors = sum(n for (_m, _r, c), n in self.http_count.items() if c == "4xx")
        return {"requests": total, "server_errors": errors, "client_errors": client_errors, "in_flight": self.in_flight}


metrics = Metrics()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(api_key|client_id|access_token|refresh_token|token|key)=([^&\s\"']+)"), r"\1=***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r"(?i)([a-z0-9-]*api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]{8,}"), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class ContextFilter(logging.Filter):
    """Adds request_id, masks credentials that slip into messages, and counts log lines per level."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        if record.levelno >= logging.WARNING:
            metrics.log_counts[record.levelname.lower()] += 1
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(env: Optional[EnvOverrides] = None) -> None:
    """PENGUWATCH_LOG_LEVEL=info|debug|..., PENGUWATCH_LOG_FORMAT=text|json (from `env`, read from the environment when
    not given). Runs at import, before config.yaml is loaded; set_level applies the config's level afterwards."""
    env = env or EnvOverrides()
    level = getattr(logging, (env.log_level or "info").upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if (env.log_format or "text").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s [%(request_id)s]: %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs every request at INFO (and puts query strings with credentials in the URL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def set_level(name: str) -> None:
    """The log level from config.yaml (server.log_level; PENGUWATCH_LOG_LEVEL has already been applied to it)."""
    logging.getLogger().setLevel(getattr(logging, (name or "info").upper(), logging.INFO))


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

class ObservabilityMiddleware:
    """Request ID + latency/error metrics + Server-Timing, without buffering the body."""

    SLOW_SECONDS = 1.0

    def __init__(self, app, registry: Optional[Metrics] = None):
        self.app = app
        self.metrics = registry or metrics
        self.logger = logging.getLogger("penguwatch.http")

    @staticmethod
    def _route_template(scope, path: str) -> str:
        """'/api/shows/42' -> '/api/shows/{show_id}'.

        Built from the matched path parameters rather than `route.path`, which in recent FastAPI
        versions is relative to the including router (no '/api' prefix). Raw paths would make the
        label cardinality unbounded, so unmatched requests collapse into one bucket.
        """
        if path == "/static" or path.startswith("/static/"):
            return "/static"
        if path == "/login":
            return "/login"
        if "endpoint" not in scope and scope.get("route") is None:
            return "/" if path == "/" else "unmatched"
        template = path
        for name, value in (scope.get("path_params") or {}).items():
            v = str(value)
            i = template.rfind("/" + v)
            if i >= 0:
                template = f"{template[:i]}/{{{name}}}{template[i + 1 + len(v):]}"
        return template

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        incoming = ""
        for name, value in scope.get("headers") or []:
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        # On FastAPI's request span, so a request id from a log line or a response header finds its trace
        server_span = trace.get_current_span()
        if server_span.is_recording():
            server_span.set_attribute("penguwatch.request_id", rid)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers") or [])
                elapsed_ms = (time.perf_counter() - start) * 1000
                headers.append((b"x-request-id", rid.encode()))
                headers.append((b"server-timing", f"app;dur={elapsed_ms:.1f}".encode()))
                message = {**message, "headers": headers}
            await send(message)

        self.metrics.in_flight += 1
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.metrics.in_flight -= 1
            seconds = time.perf_counter() - start
            path = scope.get("path", "")
            route = self._route_template(scope, path)
            method = scope.get("method", "GET")
            self.metrics.observe_http(method, route, status, seconds)
            line = f"{method} {path} -> {status} in {seconds * 1000:.0f} ms"
            if status >= 500:
                self.logger.error(line)
            elif seconds >= self.SLOW_SECONDS:
                self.logger.warning(f"slow request: {line}")
            elif method != "GET" or status >= 400:
                self.logger.info(line)
            else:
                self.logger.debug(line)
            request_id_var.reset(token)


# ---------------------------------------------------------------------------
# Prometheus text exposition
# ---------------------------------------------------------------------------

def _label_value(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kw: Any) -> str:
    return "{" + ",".join(f'{k}="{_label_value(v)}"' for k, v in kw.items()) + "}" if kw else ""


def render_prometheus(m: Metrics, gauges: Iterable[Tuple[str, str, Dict[str, Any], float]], version: str) -> str:
    """gauges: (name, help, labels, value) rows computed at scrape time."""
    out: List[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")

    family("penguwatch_build_info", "gauge", "Build information.")
    out.append(f"penguwatch_build_info{_labels(version=version)} 1")
    family("penguwatch_process_start_time_seconds", "gauge", "Unix time the process started.")
    out.append(f"penguwatch_process_start_time_seconds {m.started_at:.0f}")

    family("penguwatch_http_requests_total", "counter", "HTTP requests by route template, method and status class.")
    for (method, route, klass), n in sorted(m.http_count.items()):
        out.append(f"penguwatch_http_requests_total{_labels(method=method, route=route, status=klass)} {n}")

    family("penguwatch_http_request_duration_seconds", "histogram", "HTTP request latency by route template.")
    for route, buckets in sorted(m.http_buckets.items()):
        for bound, n in zip(HTTP_BUCKETS, buckets):
            out.append(f"penguwatch_http_request_duration_seconds_bucket{_labels(route=route, le=bound)} {n}")
        out.append(f"penguwatch_http_request_duration_seconds_bucket{_labels(route=route, le='+Inf')} {buckets[-1]}")
        out.append(f"penguwatch_http_request_duration_seconds_sum{_labels(route=route)} {m.http_sum[route]:.6f}")
        out.append(f"penguwatch_http_request_duration_seconds_count{_labels(route=route)} {buckets[-1]}")

    family("penguwatch_http_requests_in_flight", "gauge", "Requests currently being handled.")
    out.append(f"penguwatch_http_requests_in_flight {m.in_flight}")

    family("penguwatch_upstream_requests_total", "counter", "Calls to the drivers' services by endpoint and HTTP status (0 = network error).")
    for (service, endpoint, status), n in sorted(m.upstream_count.items()):
        out.append(f"penguwatch_upstream_requests_total{_labels(service=service, endpoint=endpoint, status=status)} {n}")
    family("penguwatch_upstream_request_seconds_total", "counter", "Time spent waiting on the drivers' services.")
    for (service, endpoint), secs in sorted(m.upstream_seconds.items()):
        out.append(f"penguwatch_upstream_request_seconds_total{_labels(service=service, endpoint=endpoint)} {secs:.6f}")

    family("penguwatch_webhook_deliveries_total", "counter", "Event webhook deliveries by webhook and outcome.")
    for (name, outcome), n in sorted(m.webhook_count.items()):
        out.append(f"penguwatch_webhook_deliveries_total{_labels(webhook=name, outcome=outcome)} {n}")

    family("penguwatch_log_messages_total", "counter", "Warnings and errors logged since start.")
    for level in ("warning", "error", "critical"):
        out.append(f"penguwatch_log_messages_total{_labels(level=level)} {m.log_counts.get(level, 0)}")

    seen: set = set()
    for name, help_text, labels, value in gauges:
        if name not in seen:
            family(name, "gauge", help_text)
            seen.add(name)
        out.append(f"{name}{_labels(**labels)} {value}")
    return "\n".join(out) + "\n"
