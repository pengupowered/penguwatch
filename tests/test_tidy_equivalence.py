"""get_returning_shows computes in SQL; it must give exactly what the earlier per-episode Python version gave."""

import os
import shutil
from datetime import date, timedelta

import pytest

from penguwatch import clock
from penguwatch.db.episodes import format_episode_code
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import MediaType, Show, ShowStatus
from tests.test_api import setup_test_app  # noqa: F401  (fixture)
from tests.test_tidy import _show_with_episodes

SEED = os.environ.get("PENGUWATCH_SEED_DIR")
T = date.today()


async def legacy_returning_shows(repo, completed=None):
    """The implementation before the SQL rewrite (an Episode model per episode), kept as the reference."""
    today = clock.today().isoformat()
    out = []
    if completed is None:
        completed = await repo.get_shows(status=ShowStatus.COMPLETED)
    episodes = await repo.get_episodes_by_show(ShowStatus.COMPLETED)
    for show in completed:
        if show.media_type == MediaType.MOVIE:
            continue
        eps = [e for e in episodes.get(show.id, []) if e.season > 0]
        watched = [e for e in eps if e.is_watched]
        if watched:
            last = max((e.season, e.number) for e in watched)
        elif show.episodes_synced_at and show.watched_episodes:
            ordered = sorted((e.season, e.number) for e in eps)
            last = ordered[min(show.watched_episodes, len(ordered)) - 1] if ordered else (0, 0)
        else:
            last = None
        new_eps = sorted(
            (e for e in eps if e.air_date and last is not None and (e.season, e.number) > last and not e.is_watched),
            key=lambda e: (e.season, e.number),
        )
        if not new_eps and last is None and show.next_episode_air_date and show.next_episode_air_date[:10] >= today:
            out.append({
                "show": show, "state": "upcoming", "new_count": None,
                "first_code": format_episode_code(show.next_episode_season, show.next_episode_number),
                "first_air_date": show.next_episode_air_date[:10], "new_season": show.next_episode_season,
            })
            continue
        if not new_eps:
            continue
        aired = [e for e in new_eps if e.air_date[:10] <= today]
        first = new_eps[0]
        out.append({
            "show": show,
            "state": "available" if aired else "upcoming",
            "new_count": len(new_eps),
            "aired_count": len(aired),
            "first_code": format_episode_code(first.season, first.number),
            "first_air_date": first.air_date[:10],
            "new_season": first.season if last is None or first.season > last[0] else None,
        })
    out.extend(await repo._sequel_hints(completed))
    out.sort(key=lambda r: (0 if r["state"] == "available" else 1, r["first_air_date"] or "9999"))
    return out


def _plain(rows):
    return [{**r, "show": r["show"].model_dump()} for r in rows]


async def _compare(repo):
    new = await repo.get_returning_shows()
    old = await legacy_returning_shows(repo)
    assert _plain(new) == _plain(old)
    return new


async def _raw(repo, sid, eps, **fields):
    await repo.upsert_episode_list(sid, eps)
    await repo.update_show_fields(sid, fields)


@pytest.mark.asyncio
async def test_sql_matches_the_python_version_on_varied_shows(setup_test_app):
    repo = setup_test_app
    past = T - timedelta(days=400)
    C = ShowStatus.COMPLETED
    await _show_with_episodes(repo, "Upcoming S2", C, [(6, past), (8, T + timedelta(days=20))], (1, 6))
    await _show_with_episodes(repo, "Out Now", C, [(5, past), (4, T - timedelta(days=30))], (1, 5))
    await _show_with_episodes(repo, "Mixed aired and future", C, [(3, past), (4, T - timedelta(days=2)), (3, T + timedelta(days=9))], (1, 3))
    await _show_with_episodes(repo, "Done", C, [(5, past)], (1, 5), ended=True)
    await _show_with_episodes(repo, "Watched mid-season one", C, [(5, past), (5, past)], (1, 5))
    await _show_with_episodes(repo, "Watching, not completed", ShowStatus.WATCHING, [(5, past), (4, T + timedelta(days=5))], (1, 5))
    # a movie is skipped even with episodes
    m = await repo.upsert_show(Show(title="Film", status=C, media_type=MediaType.MOVIE, ids={"simkl": 9001}))
    await _raw(repo, m, [{"season": 1, "number": 1, "title": "x", "air_date": (T + timedelta(days=3)).isoformat()}])
    # nothing watched, progress counter set and list synced: the counter points at the last watched one
    s1 = await repo.upsert_show(Show(title="Counter only", status=C, ids={"simkl": 9002}, watched_episodes=3))
    await _raw(repo, s1, [{"season": 1, "number": n, "title": "x", "air_date": (past + timedelta(days=n)).isoformat()} for n in range(1, 6)]
               + [{"season": 2, "number": 1, "title": "x", "air_date": (T + timedelta(days=3)).isoformat()}],
               episodes_synced_at="2026-01-01T00:00:00+00:00", watched_episodes=3)
    # counter larger than the list: clamps to the last episode (nothing new)
    s2 = await repo.upsert_show(Show(title="Counter too big", status=C, ids={"simkl": 9003}))
    await _raw(repo, s2, [{"season": 1, "number": n, "title": "x", "air_date": past.isoformat()} for n in range(1, 4)],
               episodes_synced_at="2026-01-01T00:00:00+00:00", watched_episodes=99)
    # counter set, list synced, but no episode rows at all: last is (0, 0), so no calendar hint either
    s3 = await repo.upsert_show(Show(title="Counter no rows", status=C, ids={"simkl": 9004}, watched_episodes=2))
    await _raw(repo, s3, [], episodes_synced_at="2026-01-01T00:00:00+00:00", watched_episodes=2,
               next_episode_air_date=(T + timedelta(days=5)).isoformat(), next_episode_season=2, next_episode_number=1)
    # counter set but the list never synced: last is unknown, the calendar hint applies, episode rows are ignored
    s4 = await repo.upsert_show(Show(title="Counter unsynced", status=C, ids={"simkl": 9005}, watched_episodes=2))
    await _raw(repo, s4, [{"season": 1, "number": n, "title": "x", "air_date": (T + timedelta(days=n)).isoformat()} for n in range(1, 4)],
               watched_episodes=2, next_episode_air_date=(T + timedelta(days=7)).isoformat(), next_episode_season=1, next_episode_number=1)
    # no episodes, calendar knows a next air date (with a time part); and one whose date has passed
    s5 = await repo.upsert_show(Show(title="Calendar only", status=C, ids={"simkl": 9006}))
    await _raw(repo, s5, [], next_episode_air_date=(T + timedelta(days=4)).isoformat() + "T20:00:00Z", next_episode_season=3, next_episode_number=1)
    s6 = await repo.upsert_show(Show(title="Calendar passed", status=C, ids={"simkl": 9007}))
    await _raw(repo, s6, [], next_episode_air_date=(T - timedelta(days=4)).isoformat(), next_episode_season=3, next_episode_number=1)
    # specials (season 0) never count, unwatched gaps before the last watched one do not count, undated episodes are ignored
    s7 = await repo.upsert_show(Show(title="Gaps and specials", status=C, ids={"simkl": 9008}))
    await _raw(repo, s7, [{"season": 0, "number": 1, "title": "sp", "air_date": (T + timedelta(days=1)).isoformat()},
                          {"season": 1, "number": 1, "title": "a", "air_date": past.isoformat()},
                          {"season": 1, "number": 2, "title": "b", "air_date": past.isoformat()},
                          {"season": 1, "number": 3, "title": "c", "air_date": past.isoformat()},
                          {"season": 1, "number": 4, "title": "d", "air_date": None},
                          {"season": 1, "number": 5, "title": "e", "air_date": ""},
                          {"season": 2, "number": 1, "title": "f", "air_date": (T + timedelta(days=2)).isoformat() + "T01:00:00Z"},
                          {"season": 2, "number": 2, "title": "g", "air_date": None}])
    await repo.set_episodes_watched(s7, [(1, 3), (0, 1)], True)
    await repo.update_show_fields(s7, {"episodes_synced_at": "2026-01-01T00:00:00+00:00"})
    # watched in a later season while an earlier season has later numbers
    s8 = await repo.upsert_show(Show(title="Season order", status=C, ids={"simkl": 9009}))
    await _raw(repo, s8, [{"season": s, "number": n, "title": "x", "air_date": (past + timedelta(days=s * 10 + n)).isoformat()}
                          for s in (1, 2, 3) for n in range(1, 4)])
    await repo.set_episodes_watched(s8, [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)], True)
    # an anime with a sequel hint
    a = await repo.upsert_show(Show(title="Anime", status=C, ids={"simkl": 9010}, media_type=MediaType.ANIME))
    import json
    await repo.update_show_fields(a, {"sequels_json": json.dumps([{"ids": {"simkl": 424242}, "title": "Anime 2", "year": 2099}])})

    rows = await _compare(repo)
    got = {r["show"].title: r for r in rows}
    # the comparison would pass for two empty lists: make sure the cases above are really exercised
    assert got["Out Now"]["state"] == "available" and got["Upcoming S2"]["state"] == "upcoming"
    assert got["Mixed aired and future"]["new_count"] == 7 and got["Mixed aired and future"]["aired_count"] == 2
    assert got["Counter only"]["first_code"] == "S01E04" and got["Counter only"]["new_count"] == 3
    assert "Counter too big" not in got and "Counter no rows" not in got and "Film" not in got and "Done" not in got
    assert got["Counter unsynced"]["new_count"] is None and got["Calendar only"]["new_count"] is None
    assert "Calendar passed" not in got
    assert got["Gaps and specials"]["first_code"] == "S02E01" and got["Gaps and specials"]["new_count"] == 1
    assert got["Season order"]["first_code"] == "S02E03" and got["Season order"]["new_count"] == 4
    assert got["Anime"]["first_code"] == "sequel:424242"


@pytest.mark.asyncio
async def test_sql_matches_the_python_version_with_completed_passed_in(setup_test_app):
    repo = setup_test_app
    await _show_with_episodes(repo, "Out Now", ShowStatus.COMPLETED, [(5, T - timedelta(days=400)), (4, T - timedelta(days=30))], (1, 5))
    await _show_with_episodes(repo, "Other", ShowStatus.COMPLETED, [(5, T - timedelta(days=400)), (4, T - timedelta(days=30))], (1, 5))
    completed = [s for s in await repo.get_shows(status=ShowStatus.COMPLETED) if s.title == "Other"]
    assert _plain(await repo.get_returning_shows(completed)) == _plain(await legacy_returning_shows(repo, completed))
    assert [r["show"].title for r in await repo.get_returning_shows(completed)] == ["Other"]


@pytest.mark.skipif(not SEED, reason="needs the sanitized seed library (scripts/test.sh --seed)")
@pytest.mark.asyncio
async def test_sql_matches_the_python_version_on_the_seed(tmp_path):
    db_file = str(tmp_path / "seed.db")
    shutil.copy(f"{SEED}/penguwatch.db", db_file)
    repo = DatabaseRepository(db_file)
    await repo.initialize()
    try:
        rows = await _compare(repo)
        assert rows, "the seed library has completed shows with something new"
    finally:
        await repo.close()
