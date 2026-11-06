"""Event webhooks: the events the data changes raise, the signed and bounded delivery, the config, and what is never shown."""

import asyncio
import json
import logging

import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

from penguwatch import webhooks_component
from penguwatch.components import Context
from penguwatch.config import AppConfig, WebhookConfig
from penguwatch.main import app, security_config
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.webhooks import Webhook, WebhookDispatcher, sign
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

SECRET = "0123456789abcdef-test-secret"
URL_TOKEN = "tok-9f8e7d6c5b4a"
URL = f"https://hooks.example.org/in/{URL_TOKEN}?k={URL_TOKEN}"


def client(**kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kw)


class Receiver:
    """An httpx transport that records requests and answers from a script of status codes."""

    def __init__(self, *statuses, headers=None, raises=None):
        self.statuses = list(statuses) or [200]
        self.headers = headers or {}
        self.raises = raises
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises:
            raise self.raises
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, headers=self.headers, content=b"x" * 10)

    @property
    def transport(self):
        return httpx.MockTransport(self)


async def no_sleep(_):
    return None


def make(receiver, *, secret=SECRET, events=None, **kw):
    hook = Webhook(name="home", url=URL, secret=secret.encode() if secret else None,
                   events=frozenset(events or ["title.added", "sync.failed", "episode.watched"]))
    return hook, WebhookDispatcher([hook], transport=receiver.transport, sleep=no_sleep, **kw)


# --- the signature -----------------------------------------------------------------------------

def test_signature_known_vector():
    # computed independently: printf '%s' '<timestamp>.<body>' | openssl dgst -sha256 -hmac '<secret>' -hex
    assert sign(SECRET.encode(), "1700000000", b'{"version":1,"type":"title.added"}') == \
        "sha256=aa6983e669ceeb386f98e2ba091b31b4b3f1e56444cb5a4a6893a02590c00db4"


def test_signature_depends_on_secret_timestamp_and_body():
    base = sign(b"k" * 16, "1", b"{}")
    assert base != sign(b"j" * 16, "1", b"{}") and base != sign(b"k" * 16, "2", b"{}") and base != sign(b"k" * 16, "1", b"{ }")


# --- delivery ----------------------------------------------------------------------------------

async def test_delivery_is_signed_with_the_timestamp_and_the_exact_body():
    rx = Receiver(200)
    hook, d = make(rx)
    assert d.publish("title.added", {"title": {"id": 1}}) == 1
    event_type, event_id, body = hook.queue.get_nowait()
    assert await d.deliver(hook, event_type, event_id, body)
    req = rx.requests[0]
    assert req.method == "POST" and str(req.url) == URL and req.content == body
    assert req.headers["x-penguwatch-event"] == "title.added" and req.headers["x-penguwatch-delivery"] == event_id
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-penguwatch-signature"] == sign(SECRET.encode(), req.headers["x-penguwatch-timestamp"], body)
    payload = json.loads(body)
    assert payload["version"] == 2 and payload["type"] == "title.added" and payload["id"] == event_id
    assert payload["data"] == {"title": {"id": 1}} and payload["created_at"]
    assert hook.delivered == 1 and hook.state == "ok"


async def test_no_secret_means_no_signature_header():
    rx = Receiver(200)
    hook, d = make(rx, secret=None)
    d.publish("title.added", {})
    assert await d.deliver(hook, *hook.queue.get_nowait())
    assert "x-penguwatch-signature" not in rx.requests[0].headers


async def test_only_subscribed_events_are_queued():
    hook, d = make(Receiver(), events=["sync.failed"])
    assert d.publish("title.added", {}) == 0 and hook.queue.qsize() == 0
    assert d.publish("sync.failed", {}) == 1 and hook.queue.qsize() == 1


async def test_server_errors_are_retried_with_pauses_then_succeed():
    rx = Receiver(503, 500, 200)
    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)

    hook, d = make(rx)
    d._sleep = sleep
    d.publish("title.added", {})
    assert await d.deliver(hook, *hook.queue.get_nowait())
    assert len(rx.requests) == 3 and pauses == list(d.retry_delays[:2])
    assert hook.delivered == 1 and hook.failed == 0 and hook.last_error is None


async def test_gives_up_after_the_last_retry_and_says_why():
    rx = Receiver(500)
    hook, d = make(rx)
    d.publish("title.added", {})
    assert not await d.deliver(hook, *hook.queue.get_nowait())
    assert len(rx.requests) == len(d.retry_delays) + 1
    assert hook.failed == 1 and hook.last_error == "HTTP 500" and hook.state == "failing"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
async def test_client_errors_are_not_retried(status):
    rx = Receiver(status)
    hook, d = make(rx)
    d.publish("title.added", {})
    assert not await d.deliver(hook, *hook.queue.get_nowait())
    assert len(rx.requests) == 1 and hook.last_error == f"HTTP {status}"


@pytest.mark.parametrize("status", [408, 429])
async def test_timeouts_and_rate_limits_are_retried(status):
    rx = Receiver(status, 200)
    hook, d = make(rx)
    d.publish("title.added", {})
    assert await d.deliver(hook, *hook.queue.get_nowait()) and len(rx.requests) == 2


async def test_network_errors_and_timeouts_are_retried_and_described_without_the_url():
    for exc, expected in ((httpx.ConnectTimeout("t"), "timeout"), (httpx.ConnectError(f"cannot reach {URL}"), "connection error (ConnectError)")):
        rx = Receiver(200, raises=exc)
        hook, d = make(rx)
        d.publish("title.added", {})
        assert not await d.deliver(hook, *hook.queue.get_nowait())
        assert len(rx.requests) == len(d.retry_delays) + 1 and hook.last_error == expected
        assert URL_TOKEN not in json.dumps(hook.describe())


async def test_a_redirect_is_not_followed():
    rx = Receiver(302, headers={"Location": "https://other.example/steal"})
    hook, d = make(rx)
    d.publish("title.added", {})
    assert not await d.deliver(hook, *hook.queue.get_nowait())
    assert [str(r.url) for r in rx.requests] == [URL]  # the Location was never requested
    assert hook.failed == 1 and "redirects are not followed" in hook.last_error


async def test_a_full_queue_drops_new_events_and_counts_them():
    hook, d = make(Receiver())
    for _ in range(hook.queue.maxsize):
        d.publish("title.added", {})
    assert d.publish("title.added", {}) == 0 and hook.dropped == 1 and hook.queue.qsize() == hook.queue.maxsize


async def test_an_oversized_body_is_not_sent():
    rx = Receiver(200)
    hook, d = make(rx, max_body=200)
    d.publish("sync.failed", {"error": "x" * 500})
    assert not await d.deliver(hook, *hook.queue.get_nowait())
    assert rx.requests == [] and hook.last_error == "payload too large"


async def test_the_answers_body_is_not_read():
    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("the response body must not be read")
            yield b""

    hook, d = make(Receiver())
    d._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Endless())),
                                  follow_redirects=False)
    d.publish("title.added", {})
    assert await d.deliver(hook, *hook.queue.get_nowait())


async def test_workers_deliver_in_the_background_and_a_dead_receiver_delays_only_itself():
    good, bad = Receiver(200), Receiver(500)
    h1 = Webhook(name="good", url="https://a.example/x", events=frozenset(["title.added"]))
    h2 = Webhook(name="bad", url="https://b.example/x", events=frozenset(["title.added"]))
    routes = {"a.example": good, "b.example": bad}
    d = WebhookDispatcher([h1, h2], transport=httpx.MockTransport(lambda r: routes[r.url.host](r)), sleep=no_sleep)
    tasks = [asyncio.create_task(w) for w in d.workers()]
    d.publish("title.added", {})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if h1.delivered and h2.failed:
            break
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert h1.delivered == 1 and h2.failed == 1 and h2.delivered == 0


async def test_send_test_reports_the_outcome():
    ok, d = make(Receiver(200))
    assert await d.send_test(ok) is True
    bad, d2 = make(Receiver(500))
    assert await d2.send_test(bad) is False and bad.last_error == "HTTP 500"


# --- the events the data raises --------------------------------------------------------------------

@pytest.fixture
async def seen(setup_test_app):
    repo = setup_test_app
    got = []
    repo.add_event_listener(lambda t, d: got.append((t, d)))
    return repo, got


async def test_adding_a_title_raises_title_added_once(seen):
    repo, got = seen
    sid = await repo.upsert_show(Show(title="Heat", year=1995, ids={"simkl": 1}, media_type=MediaType.MOVIE, status=ShowStatus.PLANTOWATCH))
    await repo.upsert_show(Show(title="Heat", year=1995, ids={"simkl": 1}, status=ShowStatus.PLANTOWATCH))  # a refresh, not an add
    assert [t for t, _ in got] == ["title.added"]
    assert got[0][1]["title"] == {"id": sid, "title": "Heat", "year": 1995, "media_type": "movie", "ids": {"simkl": 1}}


async def test_status_and_rating_changes_raise_events_from_every_write_path(seen):
    repo, got = seen
    sid = await repo.upsert_show(Show(title="Heat", ids={"simkl": 1}, status=ShowStatus.PLANTOWATCH))
    got.clear()
    await repo.update_show_metadata(sid, status=ShowStatus.WATCHING)                       # the API's path
    await repo.update_show_fields(sid, {"status": "completed", "user_rating": 9.0})        # the sync engine's path
    await repo.upsert_show(Show(title="Heat", ids={"simkl": 1}, status=ShowStatus.DROPPED))      # a pull
    await repo.update_show_metadata(sid, user_rating=7.0)
    await repo.update_show_metadata(sid, clear_user_rating=True)
    await repo.update_show_metadata(sid, status=ShowStatus.DROPPED)                        # no change: no event
    kinds = [(t, d.get("from"), d.get("to")) for t, d in got]
    assert kinds == [
        ("title.status_changed", "plantowatch", "watching"),
        ("title.status_changed", "watching", "completed"), ("title.rating_changed", None, 9.0),
        ("title.status_changed", "completed", "dropped"),
        ("title.rating_changed", 9.0, 7.0), ("title.rating_changed", 7.0, None),
    ]
    assert got[0][1]["title"]["id"] == sid and got[2][1]["source"] == "user"


async def test_marking_episodes_watched_raises_one_event_for_the_new_ones(seen):
    repo, got = seen
    sid = await repo.upsert_show(Show(title="Heat", ids={"simkl": 1}))
    got.clear()
    await repo.set_episodes_watched(sid, [(1, 1), (1, 2)], True)
    await repo.set_episodes_watched(sid, [(1, 2), (1, 3)], True)   # (1,2) was already watched
    await repo.set_episodes_watched(sid, [(1, 1)], True)           # nothing new
    await repo.set_episodes_watched(sid, [(1, 1)], False)          # unwatching is not an event
    assert [t for t, _ in got] == ["episode.watched", "episode.watched"]
    assert [e["code"] for e in got[0][1]["episodes"]] == ["S01E01", "S01E02"]
    assert [e["code"] for e in got[1][1]["episodes"]] == ["S01E03"] and got[1][1]["count"] == 1


async def test_custom_rating_changes_raise_events(seen):
    from penguwatch.ratings import RatingSourceSpec, RatingValue
    repo, got = seen
    sid = await repo.upsert_show(Show(title="Heat", ids={"simkl": 1}))
    await repo.create_rating_source(RatingSourceSpec(id="lb", name="Letterboxd", label="LB", scale_max=5))
    got.clear()
    await repo.set_title_ratings("lb", {sid: RatingValue(value=3.0)})
    await repo.set_title_ratings("lb", {sid: RatingValue(value=3.0)})   # unchanged
    await repo.set_title_ratings("lb", {sid: RatingValue(value=4.5)})
    assert [(t, d["source"], d["from"], d["to"]) for t, d in got] == [
        ("title.rating_changed", "lb", None, 3.0), ("title.rating_changed", "lb", 3.0, 4.5)]


async def test_a_rolled_back_transaction_publishes_nothing(seen):
    repo, got = seen
    with pytest.raises(RuntimeError):
        async with repo.write() as db:
            await db.execute("INSERT INTO shows (title, simkl_id) VALUES ('Ghost', 9)")
            repo.queue_event("title.added", {"title": {"id": 1}})
            raise RuntimeError("abort")
    assert got == []
    async with repo.write():
        repo.queue_event("title.added", {"title": {"id": 2}})
    assert [d["title"]["id"] for _, d in got] == [2]


async def test_a_failing_listener_does_not_break_the_write(setup_test_app, caplog):
    repo = setup_test_app

    def boom(*_):
        raise ValueError("listener bug")

    repo.add_event_listener(boom)
    with caplog.at_level(logging.ERROR):
        sid = await repo.upsert_show(Show(title="Heat", ids={"simkl": 1}))
    assert sid and "an event listener failed" in caplog.text


async def test_sync_failed_is_raised_for_a_new_error_only(container, seen):
    from penguwatch.drivers.simkl.client import SimklAuthError  # noqa: F401
    repo, got = seen
    sync = container.sync

    async def fail(*a, **k):
        raise RuntimeError("SIMKL exploded")

    sync.flush_outbox = fail
    got.clear()
    first = await sync.sync(trigger="manual")
    second = await sync.sync(trigger="scheduled")   # the same error again
    assert first["status"] == second["status"] == "error"
    assert [t for t, _ in got] == ["sync.failed"]
    assert got[0][1]["error"] == "SIMKL exploded" and got[0][1]["trigger"] == "manual"


# --- the component -------------------------------------------------------------------------------

def cfg(**over):
    return WebhookConfig(**{"name": "home", "url": URL, **over})


def test_secret_from_environment_file_or_missing(tmp_path):
    assert webhooks_component.resolve_secret(cfg(), {}) == (None, None)
    assert webhooks_component.resolve_secret(cfg(secret_env="HOOK"), {"HOOK": SECRET}) == (SECRET.encode(), None)
    secret, why = webhooks_component.resolve_secret(cfg(secret_env="HOOK"), {})
    assert secret is None and why == "environment variable HOOK is not set"
    secret, why = webhooks_component.resolve_secret(cfg(secret_env="HOOK"), {"HOOK": "short"})
    assert secret is None and "shorter than 16" in why and "short" not in why.replace("shorter", "")
    f = tmp_path / "s"
    f.write_text(SECRET + "\n")
    assert webhooks_component.resolve_secret(cfg(secret_file=str(f)), {}) == (SECRET.encode(), None)
    assert "cannot be read" in webhooks_component.resolve_secret(cfg(secret_file=str(tmp_path / "nope")), {})[1]
    f.write_text("  \n")
    assert "is empty" in webhooks_component.resolve_secret(cfg(secret_file=str(f)), {})[1]


async def test_a_webhook_whose_secret_is_missing_stays_off_and_never_sends_unsigned(container, caplog, monkeypatch):
    monkeypatch.delenv("MISSING_HOOK_SECRET", raising=False)
    container.config.webhooks = [cfg(secret_env="MISSING_HOOK_SECRET")]
    ctx = Context(app=app, config=container.config)
    ctx.container, ctx.repository = container, container.repository
    with caplog.at_level(logging.ERROR):
        component = await webhooks_component.register(ctx)
    assert "MISSING_HOOK_SECRET is not set" in caplog.text
    hook = container.webhooks.hooks[0]
    assert hook.state == "disabled" and container.webhooks.publish("title.added", {}) == 0
    assert container.repository._event_listeners == []
    await component.stop()


async def test_the_component_wires_events_to_deliveries_and_unwires_on_stop(container, monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    container.config.webhooks = [cfg(secret_env="HOOK_SECRET", events=["title.added"])]
    ctx = Context(app=app, config=container.config)
    ctx.container, ctx.repository = container, container.repository
    component = await webhooks_component.register(ctx)
    rx = Receiver(200)
    container.webhooks._client = httpx.AsyncClient(transport=rx.transport, follow_redirects=False)
    await component.start()
    await container.repository.upsert_show(Show(title="Heat", ids={"simkl": 1}))
    await container.repository.update_show_metadata(1, status=ShowStatus.COMPLETED)   # not subscribed
    for _ in range(100):
        await asyncio.sleep(0.01)
        if rx.requests:
            break
    assert len(rx.requests) == 1 and rx.requests[0].headers["x-penguwatch-event"] == "title.added"
    for t in list(ctx.tasks):
        t.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    await component.stop()
    assert container.repository._event_listeners == []


async def test_airs_today_is_published_once_per_episode(container):
    from datetime import timedelta
    from penguwatch import clock
    repo = container.repository
    today = clock.today().isoformat()
    sid = await repo.upsert_show(Show(title="Tonight", ids={"simkl": 5}, status=ShowStatus.WATCHING))
    await repo.upsert_episode_list(sid, [{"season": 2, "number": 4, "title": "Four", "air_date": today},
                                         {"season": 2, "number": 5, "title": "Five", "air_date": (clock.today() + timedelta(days=7)).isoformat()}])
    hook = Webhook(name="h", url=URL, events=frozenset(["episode.airs_today"]))
    d = WebhookDispatcher([hook], transport=Receiver().transport)
    assert await webhooks_component.publish_airs_today(repo, d) == 1
    assert await webhooks_component.publish_airs_today(repo, d) == 0     # already announced
    _, _, body = hook.queue.get_nowait()
    data = json.loads(body)["data"]
    assert data["title"]["title"] == "Tonight" and (data["season"], data["number"]) == (2, 4) and data["air_date"] == today
    quiet = WebhookDispatcher([Webhook(name="q", url=URL, events=frozenset(["sync.failed"]))], transport=Receiver().transport)
    assert await webhooks_component.publish_airs_today(repo, quiet) == 0


# --- config ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"url": "ftp://x.example/y"}, {"url": "not a url"}, {"url": "https://"}, {"events": ["title.exploded"]}, {"events": []},
    {"secret_env": "my secret"}, {"secret_env": SECRET + "!"}, {"secret_env": "A", "secret_file": "/x"},
    {"name": "bad/name"}, {"name": ""},
])
def test_webhook_config_is_validated(bad):
    with pytest.raises(ValidationError):
        WebhookConfig(**{"url": URL, **bad})


def test_webhook_names_default_to_the_host_and_must_be_unique():
    config = AppConfig(webhooks=[{"url": "https://a.example/1"}, {"url": "https://b.example/1"}])
    assert [h.name for h in config.webhooks] == ["a.example", "b.example"]
    assert config.webhooks[0].events and "sync.failed" in config.webhooks[0].events
    with pytest.raises(ValidationError):
        AppConfig(webhooks=[{"url": "https://a.example/1"}, {"url": "https://a.example/2"}])


# --- nothing secret is ever shown ------------------------------------------------------------------

async def test_secrets_and_urls_appear_nowhere(container, caplog, tmp_path):
    hook = Webhook(name="home", url=URL, secret=SECRET.encode(), events=frozenset(["title.added"]))
    d = WebhookDispatcher([hook], transport=Receiver(500, raises=None).transport, sleep=no_sleep)
    container.webhooks = d
    await container.repository.upsert_show(Show(title="Heat", ids={"simkl": 1}))
    d.publish("title.added", {})
    with caplog.at_level(logging.DEBUG):
        await d.deliver(hook, *hook.queue.get_nowait())      # fails: logs a warning
        await d.send_test(hook)
    async with client() as ac:
        bodies = [
            (await ac.get("/api/webhooks")).text, (await ac.get("/api/system")).text, (await ac.get("/metrics")).text,
            (await ac.get("/api/export/library.csv")).text, (await ac.get("/api/openapi.json")).text,
            (await ac.get("/openapi.json")).text, (await ac.get("/api/webhooks/events")).text,
            (await ac.post("/api/webhooks/home/test")).text, (await ac.get("/api/stats")).text,
            (await ac.get("/api/shows")).text,
        ]
        backup = (await ac.get("/api/export/backup.db")).content
    everything = "\n".join(bodies) + caplog.text
    for needle in (SECRET, URL_TOKEN, "hooks.example.org/in"):
        assert needle not in everything, needle
        assert needle.encode() not in backup, needle
    assert "hooks.example.org" in bodies[0]            # the host alone is shown
    assert '"signed":true' in bodies[0].replace(" ", "")


# --- API guards --------------------------------------------------------------------------------

WEBHOOK_ROUTES = [("GET", "/api/webhooks"), ("GET", "/api/webhooks/events"), ("POST", "/api/webhooks/home/test")]


@pytest.fixture
def token_mode(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    return {"Authorization": "Bearer s3cret-token"}


@pytest.mark.parametrize("method,path", WEBHOOK_ROUTES)
async def test_every_webhook_route_needs_the_token(container, token_mode, method, path):
    async with client() as ac:
        assert (await ac.request(method, path)).status_code == 401
        assert (await ac.request(method, path, headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await ac.request(method, path, headers=token_mode)).status_code != 401


async def test_test_delivery_needs_the_apps_origin(container):
    rx = Receiver(200)
    hook, d = make(rx)
    container.webhooks = d
    async with client() as ac:
        assert (await ac.post("/api/webhooks/home/test", headers={"Origin": "http://evil.example"})).status_code == 403
        assert rx.requests == []
        ok = await ac.post("/api/webhooks/home/test", headers={"Origin": "http://test"})
        assert ok.status_code == 200 and ok.json() == {"delivered": True, "error": None}
        assert (await ac.post("/api/webhooks/none/test")).status_code == 404


async def test_webhooks_show_on_the_system_tab_and_in_metrics(container):
    rx = Receiver(500)
    hook, d = make(rx)
    container.webhooks = d
    d.publish("title.added", {})
    await d.deliver(hook, *hook.queue.get_nowait())
    d.publish("title.added", {})
    async with client() as ac:
        system = (await ac.get("/api/system")).json()["webhooks"]
        metrics = (await ac.get("/metrics")).text
    assert system["hooks"][0]["name"] == "home" and system["hooks"][0]["state"] == "failing"
    assert system["hooks"][0]["failed"] == 1 and system["hooks"][0]["queued"] == 1
    assert system["recent"][0]["outcome"] == "failed" and system["recent"][0]["detail"] == "HTTP 500"
    assert 'penguwatch_webhook_deliveries_total{webhook="home",outcome="failed"}' in metrics
    assert 'penguwatch_webhook_queue_depth{webhook="home"} 1' in metrics


# --- the whole thing, as the app runs it ------------------------------------------------------------

async def test_end_to_end_config_lifespan_signed_delivery_to_a_real_receiver(tmp_path, monkeypatch):
    """config.yaml -> lifespan -> a title added through the API -> a signed POST to a real HTTP server, checked by the
    example receiver's own verify(); the same run imports ratings into a source defined in the config."""
    import importlib.util
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    from penguwatch import main

    spec = importlib.util.spec_from_file_location("rx", Path(__file__).resolve().parent.parent / "docs" / "examples" / "receive_webhook.py")
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)

    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.headers["X-PenguWatch-Event"], rx.verify(SECRET.encode(), self.headers, body), json.loads(body)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = tmp_path / "config.yaml"
    config.write_text(f"""
ratings:
  sources:
    - {{id: letterboxd, name: Letterboxd, label: LB, scale_min: 0, scale_max: 5, show_on_cards: true}}
webhooks:
  - name: local
    url: http://127.0.0.1:{server.server_port}/in
    events: [title.added, ratings.imported]
    secret_env: E2E_HOOK_SECRET
""", encoding="utf-8")
    monkeypatch.setenv("PENGUWATCH_CONFIG", str(config))
    monkeypatch.setenv("PENGUWATCH_DB_PATH", str(tmp_path / "data" / "app.db"))
    monkeypatch.setenv("E2E_HOOK_SECRET", SECRET)
    for var in ("SIMKL_ACCESS_TOKEN", "SIMKL_CLIENT_ID", "PENGUWATCH_NOTIFY_URL"):
        monkeypatch.delenv(var, raising=False)
    try:
        async with main.lifespan(main.app):
            async with client() as ac:
                made = await ac.post("/api/shows", json={"title": "Heat", "year": 1995, "status": "plantowatch"})
                assert made.status_code == 200
                sid = made.json()["id"]
                sources = (await ac.get("/api/rating-sources")).json()
                assert sources[-1]["id"] == "letterboxd" and sources[-1]["read_only"]
                done = await ac.put("/api/ratings/letterboxd", json={"items": [{"match": {"title": "heat", "year": 1995}, "value": 4.5}]})
                assert done.json()["updated"] == 1
                await ac.patch(f"/api/shows/{sid}", json={"priority": "high"})   # not subscribed: no event
                for _ in range(200):
                    if len(got) >= 2:
                        break
                    await asyncio.sleep(0.02)
                state = (await ac.get("/api/webhooks")).json()[0]
    finally:
        server.shutdown()
    assert [(e, ok) for e, ok, _ in got] == [("title.added", True), ("ratings.imported", True)]
    assert got[0][2]["data"]["title"]["title"] == "Heat" and got[1][2]["data"] == {"source": "letterboxd", "updated": 1, "unchanged": 0}
    assert state["name"] == "local" and state["delivered"] == 2 and state["state"] == "ok" and state["signed"]


# --- the security review's findings -------------------------------------------------------------------

def test_a_validation_error_never_echoes_the_secret_or_the_address():
    leaky = "SuperSecretValueThatIsLongEnough1234"
    for hooks in ([{"url": f"https://h.example/{leaky}", "events": ["nope"]}],
                  [{"url": "https://h.example/x", "secret_env": leaky + " with spaces"}],
                  [{"url": f"https://h.example:99999/{leaky}"}]):
        with pytest.raises(ValidationError) as e:
            AppConfig(webhooks=hooks)
        assert leaky not in str(e.value) and leaky not in repr(e.value)
        with pytest.raises(ValidationError) as direct:   # the model on its own, too
            WebhookConfig(**hooks[0])
        assert leaky not in str(direct.value)


@pytest.mark.parametrize("url", ["http://h.example:99999/", "http://h.example:0x/", "http://h.example:-1/"])
def test_a_bad_port_is_refused_at_load(url):
    with pytest.raises(ValidationError):
        WebhookConfig(url=url)
    assert WebhookConfig(url="http://h.example:8123/x").url.endswith(":8123/x")


def test_a_secret_pasted_into_secret_env_is_not_repeated_in_the_reason():
    pasted = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"      # a valid identifier, but not an env-var-looking name
    secret, why = webhooks_component.resolve_secret(cfg(secret_env=pasted), {pasted: "x" * 20})
    assert secret is None and pasted not in why and "not a variable name" in why
    assert webhooks_component.resolve_secret(cfg(secret_env="A" * 65), {})[0] is None
    assert "MY_HOOK_SECRET" in webhooks_component.resolve_secret(cfg(secret_env="MY_HOOK_SECRET"), {})[1]


def test_a_secret_file_must_be_a_regular_file(tmp_path):
    assert "not a regular file" in webhooks_component.resolve_secret(cfg(secret_file=str(tmp_path)), {})[1]
    assert "not a regular file" in webhooks_component.resolve_secret(cfg(secret_file="/dev/zero"), {})[1]


async def test_a_receiver_that_never_finishes_is_cut_off_by_the_overall_deadline():
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    hook = Webhook(name="slow", url=URL, events=frozenset(["title.added"]))
    d = WebhookDispatcher([hook], transport=httpx.MockTransport(slow), timeout=0.05, retry_delays=(), sleep=no_sleep)
    d.publish("title.added", {})
    started = asyncio.get_running_loop().time()
    assert not await d.deliver(hook, *hook.queue.get_nowait())
    assert asyncio.get_running_loop().time() - started < 2 and hook.last_error == "timeout"
    assert d.recent[-1]["event"] == "title.added" and d.recent[-1]["outcome"] == "failed"   # the entry names its event


async def test_the_client_ignores_proxy_and_ca_variables(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    d = WebhookDispatcher([], transport=None)
    assert d._client.trust_env is False
    await d.close()


async def test_a_full_queue_shows_the_dropping_state_for_a_while():
    hook, d = make(Receiver())
    for _ in range(hook.queue.maxsize + 1):
        d.publish("title.added", {})
    assert hook.state == "dropping" and hook.describe()["last_dropped_at"]
    hook.last_dropped_at -= 3600
    assert hook.state == "idle"


async def test_dropping_is_visible_on_the_api(container):
    hook, d = make(Receiver())
    container.webhooks = d
    for _ in range(hook.queue.maxsize + 1):
        d.publish("title.added", {})
    async with client() as ac:
        assert (await ac.get("/api/webhooks")).json()[0]["state"] == "dropping"
        assert (await ac.get("/api/system")).json()["webhooks"]["hooks"][0]["state"] == "dropping"


async def test_the_first_full_sync_sends_one_summary_not_an_event_per_title(container, seen):
    repo, got = seen
    sync = container.sync

    async def pull(force_full=False):
        for i in range(30):
            await repo.upsert_show(Show(title=f"T{i}", ids={"simkl": 100 + i}, status=ShowStatus.WATCHING))
        await repo.update_show_fields(1, {"status": "completed"})
        return {"mode": "full", "touched_ids": []}

    async def nothing(*a, **k):
        return 0

    sync._pull, sync.flush_outbox = pull, nothing
    sync.enrich_titles = sync.refresh_episode_lists = sync.refresh_sequels = sync.sync_upcoming_schedule = nothing
    got.clear()
    result = await sync.sync(trigger="manual")
    assert result["status"] == "success"
    assert got == [("library.imported", {"count": 30})]
    await repo.set_state("driver.simkl.last_sync", "2026-11-04T00:00:00+00:00")   # now it is not the first any more
    got.clear()
    await repo.upsert_show(Show(title="Later", ids={"simkl": 999}))
    assert [t for t, _ in got] == ["title.added"] and repo._events_muted == 0


async def test_a_cancelled_first_sync_does_not_leave_events_muted(container, seen):
    repo, got = seen

    async def hang(*a, **k):
        raise asyncio.CancelledError()

    container.sync.flush_outbox = hang
    with pytest.raises(asyncio.CancelledError):
        await container.sync.sync()
    assert repo._events_muted == 0


async def test_old_airs_today_markers_are_cleaned_up(container):
    repo = container.repository
    await repo.set_state("webhook:airs:1:1:1:2020-01-01", "2020-01-01")
    await repo.set_state("webhook:airs:1:1:2:2999-01-01", "2999-01-01")
    await repo.set_state("driver.simkl.last_sync", "2020-01-01")
    hook = Webhook(name="h", url=URL, events=frozenset(["episode.airs_today"]))
    await webhooks_component.publish_airs_today(repo, WebhookDispatcher([hook], transport=Receiver().transport))
    keys = {r["key"] for r in await (await (await repo.read_connection()).execute("SELECT key FROM app_state")).fetchall()}
    assert "webhook:airs:1:1:1:2020-01-01" not in keys and "webhook:airs:1:1:2:2999-01-01" in keys and "driver.simkl.last_sync" in keys


async def test_the_docs_page_is_off_because_the_policy_forbids_its_scripts(container):
    async with client() as ac:
        assert (await ac.get("/docs")).status_code == 404
        assert (await ac.get("/redoc")).status_code == 404
        assert (await ac.get("/openapi.json")).status_code == 200
