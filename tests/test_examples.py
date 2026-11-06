"""The scripts docs/extending.md shows are run here: against the real app, and against the real signature."""

import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from penguwatch.main import app
from penguwatch.models import Show
from penguwatch.webhooks import Webhook, WebhookDispatcher, sign
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"


def load(name):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_examples_are_valid_python_and_documented():
    for path in EXAMPLES.glob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    doc = (EXAMPLES.parent / "extending.md").read_text(encoding="utf-8")
    assert "examples/push_ratings.py" in doc and "examples/receive_webhook.py" in doc


def test_the_snippet_in_the_docs_is_the_receivers_verify_function():
    """docs/extending.md shows verify() in full: it must be the one the example file defines."""
    doc = (EXAMPLES.parent / "extending.md").read_text(encoding="utf-8")
    shown = doc.split("def verify(", 1)[1].split("```", 1)[0]
    real = (EXAMPLES / "receive_webhook.py").read_text(encoding="utf-8").split("def verify(", 1)[1].split("\n\n\ndef make_handler", 1)[0]
    body = lambda s: [line.strip() for line in s.splitlines() if line.strip() and not line.strip().startswith(('"""', '#'))]  # noqa: E731
    assert [l for l in body(shown) if "TOLERANCE" not in l and "300" not in l][-3:] == [l for l in body(real) if "TOLERANCE" not in l and "300" not in l][-3:]


# --- the receiver accepts what the dispatcher signs, and nothing else --------------------------------

def test_receiver_accepts_the_apps_signature_and_refuses_everything_else():
    receiver = load("receive_webhook")
    secret, body, now = b"0123456789abcdef-test-secret", b'{"version":1,"type":"title.added"}', 1_700_000_000
    headers = {"X-PenguWatch-Timestamp": str(now), "X-PenguWatch-Signature": sign(secret, str(now), body)}
    assert receiver.verify(secret, headers, body, now=now)
    assert receiver.verify(secret, headers, body, now=now + 299)
    assert not receiver.verify(secret, headers, body, now=now + 301)              # a replay later
    assert not receiver.verify(secret, headers, body + b" ", now=now)             # a changed body
    assert not receiver.verify(b"another-secret-0123456", headers, body, now=now)
    assert not receiver.verify(secret, {**headers, "X-PenguWatch-Signature": "sha256=" + "0" * 64}, body, now=now)
    assert not receiver.verify(secret, {**headers, "X-PenguWatch-Signature": ""}, body, now=now)
    assert not receiver.verify(secret, {"X-PenguWatch-Signature": headers["X-PenguWatch-Signature"]}, body, now=now)
    assert not receiver.verify(secret, {**headers, "X-PenguWatch-Timestamp": "1e9"}, body, now=now)
    assert not receiver.verify(secret, {**headers, "X-PenguWatch-Timestamp": "١٧٠٠٠٠٠٠٠٠"}, body, now=now)   # Unicode digits
    assert not receiver.verify(secret, {**headers, "X-PenguWatch-Signature": "sha256=\u00e9"}, body, now=now)  # no TypeError


async def test_receiver_verifies_a_real_delivery():
    receiver = load("receive_webhook")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(receiver.verify(b"0123456789abcdef-test-secret", request.headers, request.content, now=time.time()))
        return httpx.Response(204)

    hook = Webhook(name="h", url="https://x.example/in", secret=b"0123456789abcdef-test-secret", events=frozenset(["title.added"]))
    d = WebhookDispatcher([hook], transport=httpx.MockTransport(handler))
    d.publish("title.added", {"title": {"id": 1}})
    assert await d.deliver(hook, *hook.queue.get_nowait()) and seen == [True]


def test_receiver_handler_answers_401_without_a_valid_signature():
    receiver = load("receive_webhook")
    from http.server import HTTPServer
    import threading
    server = HTTPServer(("127.0.0.1", 0), receiver.make_handler(b"0123456789abcdef-test-secret"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        assert httpx.post(url, content=b"{}", headers={"X-PenguWatch-Signature": "sha256=00"}).status_code == 401
        body = b'{"type":"title.added","data":{}}'
        ts = str(int(time.time()))
        ok = httpx.post(url, content=body, headers={"X-PenguWatch-Timestamp": ts, "X-PenguWatch-Signature": sign(b"0123456789abcdef-test-secret", ts, body)})
        assert ok.status_code == 204
    finally:
        server.shutdown()


# --- the ratings script runs against the app --------------------------------------------------------

async def test_push_ratings_example_against_the_app(setup_test_app, tmp_path):
    repo = setup_test_app
    await repo.upsert_show(Show(title="Heat", year=1995, ids={"simkl": 1, "imdb": "tt0113277"}))
    await repo.upsert_show(Show(title="Alien", year=1979, ids={"simkl": 2}))
    pushed = load("push_ratings")
    csv_path = tmp_path / "r.csv"
    csv_path.write_text("title,year,rating,imdb_id\nHeat,1995,4.5,tt0113277\nAlien,1979,5,\nNo Such Film,2001,3,\n", encoding="utf-8")

    import asyncio
    client = TestClient(app, base_url="http://test")
    result = await asyncio.to_thread(pushed.main, ["push_ratings.py", "letterboxd", str(csv_path)], {}, client)
    assert result == 0
    rows = {r["source"]: r for r in (await asyncio.to_thread(client.get, "/api/shows/1/ratings")).json()}
    assert rows["letterboxd"]["value"] == 4.5
    shows = {s["title"]: s for s in (await asyncio.to_thread(client.get, "/api/shows")).json()}
    assert shows["Alien"]["ratings"]["letterboxd"]["value"] == 5.0
    # run again: nothing changes
    again = await asyncio.to_thread(pushed.push, client, "letterboxd", list(pushed.read_items([
        {"title": "Heat", "year": "1995", "rating": "4.5", "imdb_id": "tt0113277"}])))
    assert again["updated"] == 0 and again["unchanged"] == 1


def test_push_ratings_example_reads_every_kind_of_match():
    pushed = load("push_ratings")
    items = list(pushed.read_items([
        {"title": "A", "year": "2000", "rating": "3"}, {"title": "B", "rating": "4", "votes": "10"},
        {"title": "C", "rating": "1", "imdb_id": "tt1"}, {"title": "D", "rating": "2", "tmdb_id": "55"}]))
    assert items == [
        {"match": {"title": "A", "year": 2000}, "value": 3.0}, {"match": {"title": "B"}, "value": 4.0, "votes": 10},
        {"match": {"imdb": "tt1"}, "value": 1.0}, {"match": {"tmdb": 55}, "value": 2.0}]
    from penguwatch.ratings import RatingImportRequest
    RatingImportRequest(items=[{"match": i["match"], **{k: v for k, v in i.items() if k != "match"}} for i in items[:2]])
