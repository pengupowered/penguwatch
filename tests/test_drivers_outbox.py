"""Pending changes survive the driver layer and reach SIMKL byte for byte as before.

tests/data/drivers/push_golden.json holds every request the pre-driver code sent when it delivered the outbox of
tests/data/drivers/pre_drivers.sql (written by tests/data/drivers/generate_pre_drivers.py on the commit before the
driver layer). Two ways must send exactly those requests (method, path, query and body bytes, in order):

- that database migrated: the queued rows are neutral operations now, still delivered as they were;
- the same edits made today, on a fresh library: neutral operations built and turned into SIMKL bodies.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from penguwatch import clock
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import MediaType, ServiceConfig, Show, ShowStatus
from penguwatch.ratings import RatingSourceSpec, RatingValue
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient
from tests.simkl_helpers import simkl_engine

DATA = Path(__file__).parent / "data" / "drivers"
GOLDEN = json.loads((DATA / "push_golden.json").read_text(encoding="utf-8"))
FROZEN = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: FROZEN)


async def deliver(repo):
    """Approves everything, releases the back-off, flushes; returns (pushed, requests sent, rows left)."""
    client = SimklClient(client_id="cid", access_token="tok")
    sync = simkl_engine(client, repo)
    sent = []

    def answer(request):
        sent.append({"method": request.method, "path": request.url.path, "query": request.url.query.decode("ascii"),
                     "body": request.content.decode("utf-8")})
        return httpx.Response(201, json={"not_found": {"shows": [], "movies": []}})

    try:
        with respx.mock(assert_all_mocked=True) as mock:
            mock.post(url__startswith=SIMKL_BASE_URL).mock(side_effect=answer)
            await repo.approve_outbox(None)
            await repo.release_outbox_backoff()
            pushed = await sync.flush_outbox()
    finally:
        await client.aclose()
    return pushed, sent, await repo.get_outbox()


async def test_the_migrated_outbox_delivers_the_same_requests_as_before(tmp_path):
    path = tmp_path / "pre.db"
    conn = sqlite3.connect(path)
    conn.executescript((DATA / "pre_drivers.sql").read_text(encoding="utf-8"))
    before = conn.execute("SELECT id, show_id, simkl_id, approved, attempts, summary FROM sync_outbox ORDER BY id").fetchall()
    conn.close()
    repo = DatabaseRepository(str(path))
    await repo.initialize()
    try:
        rows = await repo.get_outbox()
        # every pending row survived, for SIMKL, with its title, approval, attempts and summary
        assert [(r["id"], r["show_id"], r["remote_id"], int(r["approved"]), r["attempts"], r["summary"])
                for r in rows] == [tuple(b) for b in before]
        assert {r["driver"] for r in rows} == {"simkl"}
        assert all(r["operation"].native is not None for r in rows)
        pushed, sent, left = await deliver(repo)
    finally:
        await repo.close()
    assert (pushed, len(left)) == (GOLDEN["pushed"], GOLDEN["left"])
    assert sent == GOLDEN["requests"]


async def test_migrated_rows_read_as_neutral_operations(tmp_path):
    path = tmp_path / "pre.db"
    conn = sqlite3.connect(path)
    conn.executescript((DATA / "pre_drivers.sql").read_text(encoding="utf-8"))
    conn.close()
    repo = DatabaseRepository(str(path))
    await repo.initialize()
    try:
        ops = {r["id"]: r["operation"] for r in await repo.get_outbox()}
    finally:
        await repo.close()
    first, movie_rate, removal = ops[1], ops[6], ops[12]
    assert (first.kind, first.media_type, first.ids, first.episodes, first.watched_at) == (
        "watched", "show", {"simkl": 1001, "imdb": "tt0001001", "tmdb": 501}, [(1, 2), (1, 3)],
        "2026-10-10T12:00:00+00:00")
    assert (movie_rate.kind, movie_rate.media_type, movie_rate.rating) == ("rate", "movie", 8)
    assert (ops[4].kind, ops[4].status) == ("status", "hold")
    assert (removal.kind, removal.media_type, removal.ids) == ("remove", "movie", {"simkl": 3002})
    assert {op.kind for op in ops.values()} == {"watched", "unwatched", "status", "rate", "unrate", "remove"}


async def build_library(repo):
    """The library generate_pre_drivers.py builds, made with today's models."""
    shows = [
        dict(title="Alpha", ids={"simkl": 1001, "imdb": "tt0001001", "tmdb": 501, "tvdb": 701},
             media_type=MediaType.SHOW, status=ShowStatus.WATCHING, total_episodes=4, aired_episodes=4),
        dict(title="Beta", ids={"simkl": 1002, "imdb": "tt0001002"}, media_type=MediaType.SHOW,
             status=ShowStatus.WATCHING, total_episodes=10, aired_episodes=10, user_rating=6.0),
        dict(title="Gamma", ids={"simkl": 2001, "tmdb": 601}, media_type=MediaType.ANIME, status=ShowStatus.WATCHING,
             total_episodes=12, aired_episodes=12),
        dict(title="Delta", ids={"simkl": 3001, "imdb": "tt0003001", "tmdb": 801}, media_type=MediaType.MOVIE,
             status=ShowStatus.PLANTOWATCH, total_episodes=1, aired_episodes=1),
        dict(title="Epsilon", ids={"simkl": 3002}, media_type=MediaType.MOVIE, status=ShowStatus.COMPLETED,
             total_episodes=1, aired_episodes=1, watched_episodes=1),
        dict(title="Local only", ids={"tmdb": 901}, media_type=MediaType.SHOW, status=ShowStatus.WATCHING),
    ]
    ids = {spec["title"]: await repo.upsert_show(Show(**spec)) for spec in shows}
    alpha = ids["Alpha"]
    await repo.upsert_episode_list(alpha, [
        {"season": 1, "number": n, "title": f"Ep {n}", "air_date": f"2026-01-0{n}", "ids": {"simkl": 50000 + n}}
        for n in range(1, 5)])
    await repo.update_show_fields(alpha, {"episodes_synced_at": FROZEN.isoformat()})
    await repo.set_episodes_watched(alpha, [(1, 1)], True, watched_at="2026-01-05T20:00:00+00:00")
    await repo.recompute_progress_from_episodes(alpha)
    await repo.create_rating_source(RatingSourceSpec(id="letterboxd", name="Letterboxd", label="LB", scale_min=0,
                                                     scale_max=5, decimals=1))
    await repo.set_title_ratings("letterboxd", {alpha: RatingValue(value=4.5, votes=12, url="https://example.org/a")})
    return ids


async def test_the_same_edits_made_today_send_the_same_requests(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "new.db"))
    await repo.initialize([ServiceConfig(id="netflix", name="Netflix", monthly_cost=15.49, aliases=["Netflix"])])
    try:
        ids = await build_library(repo)
        alpha = ids["Alpha"]
        sync = simkl_engine(SimklClient(client_id="cid", access_token="tok"), repo)
        await repo.set_state("sync_review_mode", "off")
        await sync.set_episodes_watched(alpha, [(1, 2), (1, 3)], True)
        await sync.set_episodes_watched(ids["Gamma"], [(1, 1), (1, 2)], True)
        await sync.mark_movie_watched(ids["Delta"], True)
        await sync.update_show_status(ids["Beta"], ShowStatus.HOLD)
        await sync.update_show_rating(alpha, 8)
        await sync.update_show_rating(ids["Delta"], 7.6)
        await sync.update_show_rating(ids["Beta"], None)
        await sync.set_episodes_watched(alpha, [(1, 3)], False)
        await sync.mark_movie_watched(ids["Epsilon"], False)
        await sync.update_show_status(ids["Gamma"], ShowStatus.COMPLETED)
        await sync.update_show_status(ids["Delta"], ShowStatus.WATCHING)
        await sync.update_show_status(ids["Local only"], ShowStatus.HOLD)  # no library id: nothing queued
        await sync.remove_from_library(await repo.get_show_by_id(ids["Epsilon"]))
        await sync.mark_next_episode(alpha)
        await repo.set_state("sync_review_mode", "on")
        await sync.set_episodes_watched(ids["Beta"], [(2, 3)], True)
        await sync.update_show_rating(ids["Gamma"], 9)
        rows = await repo.get_outbox()
        assert len(rows) == 15
        backing_off = [r for r in rows if r["approved"]][1]
        await repo.fail_outbox(backing_off["id"], "SIMKL HTTP 503: down", "2099-01-01T00:00:00+00:00")
        pushed, sent, left = await deliver(repo)
    finally:
        await repo.close()
    assert (pushed, len(left)) == (GOLDEN["pushed"], GOLDEN["left"])
    assert sent == GOLDEN["requests"]


async def test_a_queued_body_the_neutral_op_cannot_rebuild_still_goes_out_unchanged(tmp_path):
    """A row written before the driver layer is sent as its original body, not rebuilt from the neutral op: a body
    with what today's builder would leave out (a TVDB id, SIMKL's anime bucket) goes out exactly as it was queued."""
    path = tmp_path / "pre.db"
    conn = sqlite3.connect(path)
    conn.executescript((DATA / "pre_drivers.sql").read_text(encoding="utf-8"))
    conn.execute("DELETE FROM sync_outbox")
    body = {"anime": [{"ids": {"simkl": 2001, "tmdb": 601, "tvdb": 9601}, "to": "watching"}], "to": "watching"}
    conn.execute("INSERT INTO sync_outbox (show_id, simkl_id, op, payload_json, approved) VALUES (3, 2001, 'list_move', ?, 1)",
                 (json.dumps(body),))
    conn.commit()
    conn.close()
    repo = DatabaseRepository(str(path))
    await repo.initialize()
    try:
        pushed, sent, left = await deliver(repo)
    finally:
        await repo.close()
    assert (pushed, left) == (1, [])
    assert [(r["path"], json.loads(r["body"])) for r in sent] == [("/sync/add-to-list", body)]
    assert sent[0]["body"] == json.dumps(body, separators=(",", ":"))
