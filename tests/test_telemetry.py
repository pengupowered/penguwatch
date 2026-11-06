"""OpenTelemetry export (telemetry.py): what is traced, what is never exported, and the Grafana links.

The app's spans use telemetry.tracer, replaced here by one on an in-memory provider; FastAPI's request spans are
checked on a small app given that provider explicitly. Metrics need the global meter provider, which can be set only
once per process, so they are checked in a child process.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from penguwatch import __version__, grafana, telemetry
from penguwatch.observability import ObservabilityMiddleware
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient

MARKER = "Marker Title Q7x"  # stands for a title or search text: must never reach an exported span or log


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(telemetry.QueryValuesRedactor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", provider.get_tracer("test"))
    yield exporter, provider
    provider.shutdown()


def _app(provider):
    app = FastAPI(telemetry={"tracer_provider": provider, "exclude": telemetry.exclude_from_telemetry,
                             "auto_configure": False, "metrics": False, "logs": False})

    @app.get("/api/titles/search")
    async def search(q: str = "", page: int = 1):
        return {"n": 0}

    @app.get("/api/calendar.ics")
    async def ics(key: str = ""):
        return {}

    for path in ("/api/health", "/login", "/static/app.js", "/metrics"):
        app.add_api_route(path, lambda: {}, methods=["GET"])
    app.add_middleware(ObservabilityMiddleware)
    return app


# --- setup --------------------------------------------------------------------------------------------------------

def test_no_endpoint_exports_nothing():
    assert not telemetry.setup_telemetry({}, set_global=False).enabled
    off = telemetry.setup_telemetry({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4318", "OTEL_SDK_DISABLED": "true"},
                                    set_global=False)
    assert not off.enabled


def test_endpoint_builds_every_signal_with_the_service_resource():
    tel = telemetry.setup_telemetry({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9/"}, set_global=False)
    try:
        assert tel.signals == ["traces", "metrics", "logs"]
        resource = tel.tracer_provider.resource.attributes
        assert resource["service.name"] == "penguwatch"  # unless OTEL_SERVICE_NAME says otherwise
        assert resource["service.version"] == __version__
        assert tel.log_handler in logging.getLogger("penguwatch").handlers
    finally:
        tel.shutdown()
    assert tel.log_handler is None
    assert not any(isinstance(h, telemetry.OtelLogHandler) for h in logging.getLogger("penguwatch").handlers)
    tel.shutdown()  # twice is fine


def test_signal_urls():
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318/", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://l/x"}
    assert telemetry._signal_url("traces", env) == "http://otel-collector:4318/v1/traces"
    assert telemetry._signal_url("logs", env) == "http://l/x"


def test_fastapi_does_not_add_its_own_exporters():
    # with auto_configure on, FastAPI would add a second exporter to our providers: every span would go out twice
    assert telemetry.fastapi_telemetry()["auto_configure"] is False


# --- request spans ------------------------------------------------------------------------------------------------

def test_query_values_never_reach_a_span(spans):
    exporter, provider = spans
    client = TestClient(_app(provider))
    client.get("/api/titles/search", params={"q": MARKER, "page": 2})
    client.get("/api/calendar.ics", params={"key": "calkey-secret-123"})
    dump = json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()])
    assert MARKER not in dump and "Marker" not in dump
    assert "calkey-secret-123" not in dump
    server = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    queries = sorted(s.attributes.get("url.query") for s in server)
    assert queries == ["key=REDACTED", "q=REDACTED&page=REDACTED"]


def test_excluded_paths_are_not_traced(spans):
    exporter, provider = spans
    client = TestClient(_app(provider))
    for path in ("/api/health", "/login", "/static/app.js", "/metrics"):
        client.get(path)
    assert exporter.get_finished_spans() == ()


def test_request_span_carries_the_request_id(spans):
    exporter, provider = spans
    resp = TestClient(_app(provider)).get("/api/titles/search", headers={"X-Request-ID": "abc123"})
    assert resp.headers["x-request-id"] == "abc123"
    server = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server[0].attributes["penguwatch.request_id"] == "abc123"
    assert server[0].attributes["http.route"] == "/api/titles/search"


# --- app spans ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_simkl_call_spans_one_per_attempt(spans):
    exporter, _ = spans
    route = respx.get(f"{SIMKL_BASE_URL}/sync/activities")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json={"all": "x"})]
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    client.on_request = telemetry.observe_upstream
    try:
        await client._request("GET", "/sync/activities")
    finally:
        await client.aclose()
    calls = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT]
    assert [s.name for s in calls] == ["SIMKL GET /sync/activities"] * 2
    assert [s.attributes["penguwatch.upstream.attempt"] for s in calls] == [1, 2]
    assert calls[0].attributes["http.response.status_code"] == 429
    assert calls[0].status.status_code == StatusCode.ERROR and calls[0].attributes["error.type"] == "429"
    assert calls[1].status.status_code != StatusCode.ERROR
    assert calls[1].attributes["url.template"] == "/sync/activities"


@pytest.mark.asyncio
@respx.mock
async def test_simkl_network_error_span_names_the_type_only(spans):
    exporter, _ = spans
    respx.get(f"{SIMKL_BASE_URL}/tv/123").mock(side_effect=httpx.ConnectError(f"refused {MARKER}"))
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    try:
        with pytest.raises(Exception):
            await client._request("GET", "/tv/123", retries=0)
    finally:
        await client.aclose()
    (call,) = exporter.get_finished_spans()
    assert call.name == "SIMKL GET /tv/{id}"  # ids collapse: bounded span names
    assert call.attributes["error.type"] == "ConnectError"
    assert MARKER not in json.dumps(dict(call.attributes)) + str(call.status.description) + str(call.events)


@pytest.mark.asyncio
async def test_traced_sync_span_gets_the_outcome(spans):
    exporter, _ = spans

    @telemetry.traced("simkl.sync", lambda *a, trigger="scheduled", **k: {"trigger": trigger})
    async def run(trigger="scheduled"):
        telemetry.record_sync_run(trigger, 0.0, {"status": "error", "mode": "delta", "duration_s": 1.5,
                                                 "pushed": 2, "updated": 3, "error": MARKER})
        return {}

    await run(trigger="manual")
    (sp,) = exporter.get_finished_spans()
    assert sp.name == "simkl.sync"
    assert sp.attributes["penguwatch.sync.trigger"] == "manual"
    assert sp.attributes["penguwatch.sync.status"] == "error"
    assert sp.attributes["penguwatch.sync.pushed"] == 2 and sp.attributes["penguwatch.sync.pulled"] == 3
    assert sp.status.status_code == StatusCode.ERROR
    assert MARKER not in json.dumps(dict(sp.attributes)) + str(sp.status.description)


@pytest.mark.asyncio
async def test_span_failure_records_the_type_not_the_message(spans):
    exporter, _ = spans
    with pytest.raises(ValueError):
        with telemetry.span("simkl.push", op="history_add", rows=2):
            raise ValueError(MARKER)
    (sp,) = exporter.get_finished_spans()
    assert sp.attributes["error.type"] == "ValueError"
    assert MARKER not in json.dumps(dict(sp.attributes)) + str(sp.status.description) + str(sp.events)


def test_span_attributes_are_a_closed_set():
    with pytest.raises(KeyError):
        telemetry._attributes({"title": MARKER})


# --- logs ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def exported_logs(spans):
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = telemetry.OtelLogHandler(provider)
    logger = logging.getLogger("penguwatch.test_telemetry")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield exporter
    logger.removeHandler(handler)
    provider.shutdown()


def _records(exporter):
    return [getattr(item, "log_record", item) for item in exporter.get_finished_logs()]


def test_logs_are_masked_tied_to_their_span_and_debug_stays_home(spans, exported_logs):
    log = logging.getLogger("penguwatch.test_telemetry")
    with telemetry.span("simkl.sync") as sp:
        log.info("calling https://api.simkl.com/x?client_id=abcdef123456&token=tok-secret-999")
        log.debug(f"Details for {MARKER} failed")
    (record,) = _records(exported_logs)
    assert "tok-secret-999" not in str(record.body) and "abcdef123456" not in str(record.body)
    assert record.attributes["logger.name"] == "penguwatch.test_telemetry"
    assert record.trace_id == sp.get_span_context().trace_id
    assert MARKER not in json.dumps([str(r.body) for r in _records(exported_logs)])


def test_uvicorn_info_lines_stay_home(exported_logs):
    provider_handler = telemetry.OtelLogHandler(LoggerProvider())
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "192.0.2.7:5555 - GET /", None, None)
    assert not provider_handler.filter(record)
    record.levelno = logging.WARNING
    assert provider_handler.filter(record)


# --- state gauges -------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_state_reads_the_same_facts_as_metrics(container):
    values = await telemetry.collect_state(container)
    assert {"titles", "episodes", "outbox", "db", "connected", "reconnect", "last_error", "review", "idle"} <= set(values)
    assert dict((a["state"], v) for v, a in values["outbox"]) == {"pending": 0, "awaiting_approval": 0, "failing": 0}
    assert values["connected"] == [(1, {"driver": "simkl"})]
    assert "last_sync_age" not in values  # never synced: absent, not -1
    assert all(v >= 0 for v, _ in values["db"])


def test_metrics_names_buckets_and_gauges_in_a_child_process():
    """With an SDK meter provider: every instrument lands under its Prometheus-bound name, a typical duration falls
    past each histogram's first bucket (the SDK's default buckets are milliseconds), and gauges read the state."""
    script = textwrap.dedent("""
        import json
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        reader = InMemoryMetricReader()
        metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
        from penguwatch import telemetry
        telemetry.observe_upstream("simkl", "/tv/123?x=1", 200, 0.4)
        telemetry.record_sync_run("scheduled", 0.0, {"status": "success", "mode": "delta", "duration_s": 12.0,
                                                     "updated": 4})
        telemetry.LOOP_LAG.record(0.02)
        telemetry.worker_beat("pulled")
        telemetry._state["values"] = {"outbox": [(3, {"state": "failing"})], "titles": [(7, {"status": "watching"})]}
        out = {}
        for rm in reader.get_metrics_data().resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    points = []
                    for p in m.data.data_points:
                        d = {"attrs": dict(p.attributes)}
                        if hasattr(p, "bucket_counts"):
                            d["first_bucket"] = p.bucket_counts[0]
                            d["count"] = p.count
                        else:
                            d["value"] = p.value
                        points.append(d)
                    out[m.name] = points
        print(json.dumps(out))
    """)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}  # pytest's pythonpath (src/)
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout.strip().splitlines()[-1])
    assert got["penguwatch.upstream.requests"] == [
        {"attrs": {"service": "simkl", "endpoint": "/tv/{id}", "status": "200"}, "value": 1}]
    for name in ("penguwatch.upstream.request.duration", "penguwatch.sync.duration", "penguwatch.event_loop.lag"):
        (point,) = got[name]
        assert point["count"] == 1 and point["first_bucket"] == 0, name
    assert got["penguwatch.sync.runs"][0]["attrs"] == {"trigger": "scheduled", "status": "success", "mode": "delta"}
    assert got["penguwatch.sync.changes"] == [{"attrs": {"direction": "pulled"}, "value": 4}]
    assert got["penguwatch.worker.steps"] == [{"attrs": {"step": "pulled"}, "value": 1}]
    assert got["penguwatch.outbox.items"] == [{"attrs": {"state": "failing"}, "value": 3}]
    assert got["penguwatch.library.titles"] == [{"attrs": {"status": "watching"}, "value": 7}]
    assert got["penguwatch.sync.worker.age"][0]["value"] < 5
    assert got["penguwatch.process.memory"][0]["value"] > 1_000_000
    assert {p["attrs"]["level"] for p in got["penguwatch.log.records"]} == {"warning", "error", "critical"}


# --- Grafana links ------------------------------------------------------------------------------------------------

def test_grafana_links():
    assert grafana.configure("") == []
    links = grafana.configure("https://grafana.example:3000/")
    assert [link["label"] for link in links] == ["Dashboard", "Logs", "Traces"]
    assert links[0]["url"] == "https://grafana.example:3000/d/penguwatch/penguwatch"
    logs = unquote(links[1]["url"])
    assert '{service_name=\\"penguwatch\\"}' in logs and '"uid":"loki"' in logs
    traces = unquote(links[2]["url"])
    assert 'resource.service.name=\\"penguwatch\\"' in traces and '"uid":"tempo"' in traces
    assert grafana.links() == links
    grafana.configure("")


@pytest.mark.parametrize("bad", ["ftp://grafana.example", "https://u:p@grafana.example", "https://grafana.example/?a=b",
                                 "https://grafana.example/#x", "javascript:alert(1)"])
def test_grafana_url_must_be_a_plain_base_url(bad):
    with pytest.raises(ValueError):
        grafana.configure(bad)


def test_global_setup_happens_once_per_process(monkeypatch):
    # `python -m penguwatch.main` imports main twice; the lifespan can run again (tests): one set of providers
    monkeypatch.setattr(telemetry, "_installed", [])
    first = telemetry.setup_telemetry({}, set_global=True)
    assert telemetry.setup_telemetry({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}, set_global=True) is first


@pytest.mark.asyncio
async def test_simkl_answers_in_the_worker_count_as_heartbeats(monkeypatch):
    monkeypatch.setitem(telemetry._state, "worker_beat", None)
    telemetry.observe_upstream("simkl", "/search/tv", 200, 0.1)  # a page's search: not the worker
    assert telemetry._state["worker_beat"] is None

    async def in_the_worker():
        telemetry.in_worker.set(True)
        telemetry.observe_upstream("simkl", "/sync/all-items", 200, 0.1)

    await asyncio.create_task(in_the_worker())  # its own context, like the worker task
    assert telemetry._state["worker_beat"] is not None
    assert telemetry.in_worker.get() is False  # did not leak into this context
