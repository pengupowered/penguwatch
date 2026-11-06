"""Runs on the OLD code (main 27db691) inside the test image. Writes:

  <out>/pre_drivers.sql      a small library at schema 14 with pending outbox rows of every op, login keys
                             (placeholder tokens), staged incoming changes and caches
  <out>/push_golden.json     every SIMKL request the old code sends when that outbox is approved and flushed
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import httpx
import respx

OUT = sys.argv[1]
FROZEN = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

from penguwatch import clock  # noqa: E402

clock.utc_now = lambda: FROZEN  # noqa: E731

from penguwatch.db.repository import DatabaseRepository  # noqa: E402
from penguwatch.models import MediaType, ServiceConfig, Show, ShowStatus  # noqa: E402
from penguwatch.simkl.client import SIMKL_BASE_URL, SimklClient  # noqa: E402
from penguwatch.simkl.sync import SimklSyncService  # noqa: E402

SHOWS = [
    dict(title="Alpha", simkl_id=1001, imdb_id="tt0001001", tmdb_id=501, tvdb_id=701, media_type=MediaType.SHOW,
         status=ShowStatus.WATCHING, total_episodes=4, aired_episodes=4),
    dict(title="Beta", simkl_id=1002, imdb_id="tt0001002", media_type=MediaType.SHOW, status=ShowStatus.WATCHING,
         total_episodes=10, aired_episodes=10, user_rating=6.0),
    dict(title="Gamma", simkl_id=2001, tmdb_id=601, media_type=MediaType.ANIME, status=ShowStatus.WATCHING,
         total_episodes=12, aired_episodes=12),
    dict(title="Delta", simkl_id=3001, imdb_id="tt0003001", tmdb_id=801, media_type=MediaType.MOVIE,
         status=ShowStatus.PLANTOWATCH, total_episodes=1, aired_episodes=1),
    dict(title="Epsilon", simkl_id=3002, media_type=MediaType.MOVIE, status=ShowStatus.COMPLETED,
         total_episodes=1, aired_episodes=1, watched_episodes=1),
    dict(title="Local only", tmdb_id=901, media_type=MediaType.SHOW, status=ShowStatus.WATCHING),
]


async def main():
    db_path = os.path.join(OUT, "pre.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass
    repo = DatabaseRepository(db_path)
    await repo.initialize([ServiceConfig(id="netflix", name="Netflix", monthly_cost=15.49, aliases=["Netflix"])])
    ids = {}
    for spec in SHOWS:
        ids[spec["title"]] = await repo.upsert_show(Show(**spec))
    alpha = ids["Alpha"]
    await repo.upsert_episode_list(alpha, [
        {"season": 1, "number": n, "title": f"Ep {n}", "air_date": f"2026-01-0{n}", "simkl_id": 50000 + n}
        for n in range(1, 5)])
    await repo.update_show_fields(alpha, {"episodes_synced_at": FROZEN.isoformat()})
    await repo.set_episodes_watched(alpha, [(1, 1)], True, watched_at="2026-01-05T20:00:00+00:00")
    await repo.recompute_progress_from_episodes(alpha)
    from penguwatch.ratings import RatingSourceSpec, RatingValue
    await repo.create_rating_source(RatingSourceSpec(id="letterboxd", name="Letterboxd", label="LB", scale_min=0,
                                                     scale_max=5, decimals=1))
    await repo.set_title_ratings("letterboxd", {alpha: RatingValue(value=4.5, votes=12, url="https://example.org/a"),
                                                ids["Delta"]: RatingValue(value=3.0)})
    await repo.update_show_fields(alpha, {"simkl_rating": 8.3, "imdb_rating": 8.1, "imdb_votes": 1200,
                                          "sequels_json": json.dumps([{"simkl_id": 2002, "title": "Gamma 2", "year": 2026,
                                                                       "poster": None, "anime_type": "tv"}])})

    client = SimklClient(client_id="cid", access_token="tok")
    sync = SimklSyncService(client=client, repository=repo)

    await repo.set_state("sync_review_mode", "off")
    await sync.set_episodes_watched(alpha, [(1, 2), (1, 3)], True)          # history_add, shows
    await sync.set_episodes_watched(ids["Gamma"], [(1, 1), (1, 2)], True)   # history_add, anime in "shows"
    await sync.mark_movie_watched(ids["Delta"], True)                       # history_add, movies
    await sync.update_show_status(ids["Beta"], ShowStatus.HOLD)             # list_move, shows
    await sync.update_show_rating(alpha, 8)                                 # rating_set
    await sync.update_show_rating(ids["Delta"], 7.6)                        # rating_set (rounded) movies
    await sync.update_show_rating(ids["Beta"], None)                        # rating_remove
    await sync.set_episodes_watched(alpha, [(1, 3)], False)                 # history_remove
    await sync.mark_movie_watched(ids["Epsilon"], False)                    # history_remove, movies
    await sync.update_show_status(ids["Gamma"], ShowStatus.COMPLETED)       # list_move
    await sync.update_show_status(ids["Delta"], ShowStatus.WATCHING)        # list_move movies (-> plantowatch)
    await sync.update_show_status(ids["Local only"], ShowStatus.HOLD)       # local only: nothing queued
    await sync.remove_from_simkl(await repo.get_show_by_id(ids["Epsilon"]))  # remove_item
    await sync.mark_next_episode(alpha)                                     # history_add via the local list
    await repo.set_state("sync_review_mode", "on")
    await sync.set_episodes_watched(ids["Beta"], [(2, 3)], True)            # waiting for approval
    await sync.update_show_rating(ids["Gamma"], 9)                          # waiting for approval
    rows = await repo.get_outbox()
    backing_off = [r for r in rows if r["approved"]][1]
    await repo.fail_outbox(backing_off["id"], "SIMKL HTTP 503: down", "2099-01-01T00:00:00+00:00")
    await repo.fail_outbox(backing_off["id"], "SIMKL HTTP 503: down", "2099-01-01T00:00:00+00:00")

    states = {
        "simkl_client_id": "placeholder-client-id",
        "simkl_access_token": "placeholder-access-token",
        "simkl_refresh_token": "placeholder-refresh-token",
        "simkl_token_expires_at": "2026-11-20T10:00:00+00:00",
        "simkl_token_renewed_at": "2026-11-13T10:00:00+00:00",
        "simkl_renewal_note": "Automatic renewal failed; reconnect before the login expires on Nov 20, 2026.",
        "simkl_scope": "media:read media:write",
        "simkl_last_sync": "2026-11-05T09:00:00+00:00",
        "simkl_last_error": "SIMKL HTTP 503: down",
        "simkl_activities": json.dumps({"all": "2026-11-05T08:00:00Z", "tv_shows": {"all": "2026-11-05T08:00:00Z"}}),
        "simkl_refresh_attempts": json.dumps({"enrich:2": "2026-11-05T08:00:00+00:00"}),
        "schedule_last_sync": "2026-11-05T09:00:00+00:00",
        "incoming_staged": json.dumps({"activities": {"all": "x"}, "at": "2026-11-05T09:00:00+00:00", "entries": [
            {"key": "show:1002", "kind": "update", "media": "show", "simkl_id": 1002, "show_id": ids["Beta"],
             "title": "Beta", "poster": None, "item": {"status": "dropped", "show": {"title": "Beta", "ids": {"simkl": 1002}}},
             "changes": [{"field": "status", "local": "hold", "remote": "dropped"}]}]}),
        "recs_cache_simkl": json.dumps({"at": "2026-11-05T09:00:00+00:00", "items": [], "seeds": []}),
        "trending:tv:week": json.dumps({"at": "2026-11-05T09:00:00+00:00", "items": []}),
        "tidy_dismissed_stale": json.dumps({str(ids["Beta"]): "2027-01-01T00:00:00+00:00"}),
    }
    await repo.set_states(states)
    await repo.close()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with open(os.path.join(OUT, "pre_drivers.sql"), "w", encoding="utf-8") as f:
        for line in conn.iterdump():
            f.write(line + "\n")
    conn.close()

    # deliver: approve everything, release the back-off, flush, record what went out
    repo = DatabaseRepository(db_path)
    await repo.initialize()
    client = SimklClient(client_id="cid", access_token="tok")
    sync = SimklSyncService(client=client, repository=repo)
    sent = []
    with respx.mock(assert_all_called=False) as mock:
        def answer(request):
            sent.append({"method": request.method, "path": request.url.path,
                         "query": str(request.url.query, "ascii") if isinstance(request.url.query, bytes) else str(request.url.query),
                         "body": request.content.decode("utf-8")})
            return httpx.Response(201, json={"not_found": {"shows": [], "movies": []}})
        mock.post(url__startswith=SIMKL_BASE_URL).mock(side_effect=answer)
        await repo.approve_outbox(None)
        await repo.release_outbox_backoff()
        pushed = await sync.flush_outbox()
    left = await repo.get_outbox()
    await client.aclose()
    await repo.close()
    with open(os.path.join(OUT, "push_golden.json"), "w", encoding="utf-8") as f:
        json.dump({"pushed": pushed, "left": len(left), "requests": sent}, f, indent=1)
    print("pushed", pushed, "requests", len(sent), "left", len(left))


asyncio.run(main())
