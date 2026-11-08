"""History tab: rolling periods, local days, mixed stamp formats, bulk marks, cost per hour (fixed clock throughout)."""

from datetime import date, datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import clock
from penguwatch.db.repository import DatabaseRepository
from penguwatch.history import parse_stamp
from penguwatch.main import app
from penguwatch.models import MediaType, ServiceConfig, Show, ShowStatus
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    clock.configure(None)
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)
    yield
    clock.configure(None)


def set_now(monkeypatch, *args):
    instant = datetime(*args, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "utc_now", lambda: instant)


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "history.db"))
    await r.initialize([ServiceConfig(id="netflix", name="Netflix", monthly_cost=30.44, is_active=True),
                        ServiceConfig(id="max", name="Max", monthly_cost=10.0, is_active=False)])
    yield r
    await r.close()


async def add_show(repo, title, stamps, runtime=60, service="netflix", genres=("Drama",), **fields):
    """A show with one watched episode per stamp (a unwatched episode when the stamp is None)."""
    sid = await repo.upsert_show(Show(ids={"simkl": abs(hash(title)) % 10**6}, title=title, runtime_minutes=runtime,
                                      service_id=service, genres=list(genres), status=ShowStatus.WATCHING, **fields))
    db = await repo.get_connection()
    await db.executemany("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 1, ?, ?, ?)",
                         [(sid, i, 1 if s else 0, s) for i, s in enumerate(stamps, start=1)])
    await db.commit()
    return sid


async def cur(repo, days=7, **kw):
    return (await repo.get_history(days, **kw))["totals"]["current"]


def test_every_stamp_shape_gives_its_local_day_and_hour():
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    assert parse_stamp("2026-03-23T04:21:46Z", timezone.utc) == (date(2026, 3, 23), 4)
    assert parse_stamp("2026-11-07T01:55:06.093604+00:00", timezone.utc) == (date(2026, 11, 7), 1)
    assert parse_stamp("2026-03-23 04:21:46", timezone.utc) == (date(2026, 3, 23), 4)  # no offset: UTC
    assert parse_stamp("2026-03-23", ny) == (date(2026, 3, 23), None)  # date-only: its own date, no hour
    assert parse_stamp("2026-03-02T03:30:00Z", ny) == (date(2026, 3, 1), 22)
    assert parse_stamp("2026-03-02T03:30:00+02:00", ny) == (date(2026, 3, 1), 20)  # another offset is converted
    assert parse_stamp("garbage", ny) is None and parse_stamp("", ny) is None and parse_stamp(None, ny) is None


@pytest.mark.asyncio
async def test_period_edges_are_whole_local_days_and_end_exclusive(repo):
    # today is 2026-11-06: the 7-day period is Oct 31..Nov 6, the previous Oct 24..Oct 30
    await add_show(repo, "Edges", [
        "2026-10-31T00:00:00Z",            # first second of the period
        "2026-11-06T23:59:59Z",            # last second of today
        "2026-11-07T00:00:00Z",            # tomorrow: out
        "2026-10-30T23:59:59Z",            # last second of the previous period
        "2026-10-24T00:00:00Z",            # first second of the previous period
        "2026-10-23T23:59:59Z",            # before both: out
    ])
    h = await repo.get_history(7)
    assert h["totals"]["current"]["episodes"] == 2 and h["totals"]["current"]["hours"] == 2.0
    assert h["totals"]["previous"]["episodes"] == 2
    assert h["period"]["start"] == "2026-10-31T00:00:00+00:00" and h["period"]["end"] == "2026-11-07T00:00:00+00:00"
    assert h["period"]["previous_start"] == "2026-10-24T00:00:00+00:00" and h["period"]["tz"] == "UTC"
    assert (await repo.get_history(30))["totals"]["current"]["episodes"] == 5  # 30 days back to Oct 8


@pytest.mark.asyncio
async def test_local_day_weekday_and_hour_not_the_utc_ones(repo, monkeypatch):
    clock.configure("America/New_York")
    set_now(monkeypatch, 2026, 3, 5, 12, 0)
    await add_show(repo, "Late", ["2026-03-02T03:30:00Z"])  # Sunday March 1st, 22:30 in New York
    h = await repo.get_history(7)
    assert h["period"]["tz"] == "America/New_York" and h["period"]["first_day"] == "2026-02-27"
    assert h["weekdays"] == [1, 0, 0, 0, 0, 0, 0] and h["weekday_start"] == "sunday"  # Sunday, not Monday
    assert h["hours_of_day"][22] == 1 and sum(h["hours_of_day"]) == 1
    day = {b["key"]: b for b in h["series"]["buckets"]}
    assert day["2026-03-01"]["episodes"] == 1 and day["2026-03-02"]["episodes"] == 0
    # the period's own edge is local midnight: 2026-03-06T04:59:59Z is still March 5th in New York (UTC-5)
    await add_show(repo, "Edge", ["2026-03-06T04:59:59Z", "2026-03-06T05:00:00Z"])
    assert (await repo.get_history(7))["totals"]["current"]["episodes"] == 2


@pytest.mark.asyncio
async def test_mixed_stamp_formats_are_all_counted_and_dates_have_no_hour(repo):
    await add_show(repo, "Mixed", [
        "2026-11-01T04:21:46Z", "2026-11-02T01:55:06.093604+00:00", "2026-11-03T20:00:00+00:00",
        "2026-11-04 08:30:00", "2026-11-05",                        # a date-only stamp
        "garbage", None,                                             # unparseable / unwatched: ignored
    ])
    h = await repo.get_history(7)
    assert h["totals"]["current"]["episodes"] == 5 and h["totals"]["current"]["active_days"] == 5
    assert sum(h["hours_of_day"]) == 4 and h["hours_of_day"][4] == 1 and h["hours_of_day"][1] == 1
    assert h["hours_of_day"][20] == 1 and h["hours_of_day"][8] == 1
    assert sum(h["weekdays"]) == 5  # the date-only one has a weekday (Thursday Nov 5)
    assert h["weekdays"][4] == 1


@pytest.mark.asyncio
async def test_bulk_groups_are_left_out_unless_asked_for(repo):
    bulk = "2026-11-02T10:00:00Z"
    await add_show(repo, "Season", [bulk] * 4, runtime=60)                       # one bulk group of 4
    await add_show(repo, "Pair", ["2026-11-03T10:00:00Z"] * 2 + ["2026-11-04"] * 3)  # 2 equal (< 3); date-only x3 is no bulk
    await add_show(repo, "Real", ["2026-11-05T21:15:00Z"])
    h = await repo.get_history(7)
    assert (h["bulk_episodes"], h["bulk_groups"]) == (4, 1)
    cur_ = h["totals"]["current"]
    assert cur_["episodes"] == 6 and cur_["hours"] == 6.0
    assert sum(h["weekdays"]) == 6 and sum(h["hours_of_day"]) == 3 and "Season" not in [s["title"] for s in h["top_shows"]]
    assert sum(b["episodes"] for b in h["series"]["buckets"]) == 6
    inc = await repo.get_history(7, include_bulk=True)
    assert inc["include_bulk"] is True and (inc["bulk_episodes"], inc["bulk_groups"]) == (4, 1)
    assert inc["totals"]["current"]["episodes"] == 10 and inc["totals"]["current"]["hours"] == 10.0
    assert sum(inc["hours_of_day"]) == 7 and "Season" in [t["title"] for t in inc["top_shows"]]
    # the same instant in another spelling is a different stored stamp: the rule is the exact stored text
    await add_show(repo, "Spelled", ["2026-11-02T10:00:00+00:00"])
    assert (await repo.get_history(7))["bulk_groups"] == 1


@pytest.mark.asyncio
async def test_movies_count_once_at_their_last_watch(repo):
    for i, (title, fields) in enumerate([
        ("Film A", dict(runtime_minutes=120, last_watched_at="2026-11-04T21:00:00Z")),
        ("Film B", dict(last_watched_at="2026-11-05T19:00:00+00:00")),                  # runtime default 110
        ("Film old", dict(runtime_minutes=90, last_watched_at="2026-09-01T21:00:00Z")),
        ("Film planned", dict(runtime_minutes=90, status=ShowStatus.PLANTOWATCH, last_watched_at="2026-11-04T21:00:00Z")),
    ]):
        fields.setdefault("status", ShowStatus.COMPLETED)
        await repo.upsert_show(Show(ids={"simkl": 500 + i}, title=title, media_type=MediaType.MOVIE, service_id="netflix", **fields))
    db = await repo.get_connection()
    await db.execute("UPDATE shows SET runtime_minutes = NULL WHERE title = 'Film B'")  # no runtime known
    await db.commit()
    h = await repo.get_history(7)
    c = h["totals"]["current"]
    assert c["movies"] == 2 and c["episodes"] == 0 and c["hours"] == round((120 + 110) / 60, 1)
    assert c["titles_finished"] == 2 and h["hours_of_day"][21] == 1 and h["hours_of_day"][19] == 1


@pytest.mark.asyncio
async def test_titles_started_and_finished(repo):
    # started: the first-ever watched episode is in the period
    await add_show(repo, "New this week", ["2026-11-03T20:00:00Z", "2026-11-04T20:00:00Z"])
    await add_show(repo, "Old favourite", ["2026-05-01T20:00:00Z", "2026-11-04T20:00:00Z"])
    await add_show(repo, "Started before", ["2026-10-26T20:00:00Z", "2026-10-27T20:00:00Z"])  # previous period
    # a bulk mark that starts a title says nothing about when it was watched
    await add_show(repo, "Bulk start", ["2026-11-02T09:00:00Z"] * 3)
    done = await add_show(repo, "Done", ["2026-11-05T20:00:00Z"])
    await repo.update_show_fields(done, {"status": ShowStatus.COMPLETED, "last_watched_at": "2026-11-05T20:00:00Z"})
    gone = await add_show(repo, "Finished long ago", ["2026-08-05T20:00:00Z"])
    await repo.update_show_fields(gone, {"status": ShowStatus.COMPLETED, "last_watched_at": "2026-08-05T20:00:00Z"})
    h = await repo.get_history(7)
    assert h["totals"]["current"]["titles_started"] == 2 and h["totals"]["current"]["titles_finished"] == 1  # New, Done
    assert h["totals"]["previous"]["titles_started"] == 1
    assert (await repo.get_history(7, include_bulk=True))["totals"]["current"]["titles_started"] == 3


@pytest.mark.asyncio
async def test_cost_per_hour_uses_the_price_in_effect_each_day(repo):
    db = await repo.get_connection()
    await db.execute("DELETE FROM service_price_history WHERE service_id = 'netflix'")
    # 30.44/month until Oct 20, 60.88/month from Oct 21 (period = Oct 8 .. Nov 6, 30 days: 13 days + 17 days)
    await db.executemany("INSERT INTO service_price_history (service_id, monthly_cost, effective_from) VALUES (?, ?, ?)",
                         [("netflix", 30.44, "2026-01-01T00:00:00+00:00"), ("netflix", 60.88, "2026-10-21T00:00:00+00:00")])
    await db.commit()
    await add_show(repo, "Hours", [f"2026-10-{d}T20:00:00Z" for d in (10, 11, 12, 13)], service="netflix")  # 4 h
    await add_show(repo, "Other", ["2026-10-15T20:00:00Z"], service="max")                                  # inactive
    await add_show(repo, "Free", ["2026-10-16T20:00:00Z"], service=None)
    by = {s["service_id"]: s for s in (await repo.get_history(30))["by_service"]}
    assert by["netflix"]["hours"] == 4.0 and by["netflix"]["active"] is True
    assert by["netflix"]["cost"] == pytest.approx(13 * 1.0 + 17 * 2.0, abs=0.01)
    assert by["netflix"]["cost_per_hour"] == pytest.approx(47 / 4, abs=0.01)
    assert by["max"]["cost"] is None and by["max"]["cost_per_hour"] is None and by["max"]["active"] is False
    assert by[""]["service"] == "Unassigned" and by[""]["cost_per_hour"] is None
    # an active, paid service nobody watched still shows its cost, with no per-hour figure
    await repo.set_service_active("max", True)
    by = {s["service_id"]: s for s in (await repo.get_history(7))["by_service"]}
    assert by["max"]["cost"] == pytest.approx(10.0 / 30.44 * 7, abs=0.01) and by["max"]["cost_per_hour"] is None


@pytest.mark.asyncio
async def test_top_lists_series_and_buckets(repo):
    await add_show(repo, "Big", [f"2026-11-0{d}T20:00:00Z" for d in range(1, 6)], genres=("Drama", "Mystery"), poster="http://p/big.jpg")
    await add_show(repo, "Small", ["2026-11-02T22:00:00Z"], genres=("Drama",), runtime=30)
    h = await repo.get_history(7)
    assert [(s["title"], s["hours"], s["episodes"]) for s in h["top_shows"]] == [("Big", 5.0, 5), ("Small", 0.5, 1)]
    assert h["top_shows"][0]["poster"] == "http://p/big.jpg" and h["top_shows"][1]["poster"] is None
    assert h["genres"] == [{"genre": "Drama", "hours": 5.5}, {"genre": "Mystery", "hours": 5.0}]
    assert h["series"]["unit"] == "day" and [b["key"] for b in h["series"]["buckets"]][0] == "2026-10-31"
    assert len(h["series"]["buckets"]) == 7 and not any(b["partial"] for b in h["series"]["buckets"])
    assert len((await repo.get_history(30))["series"]["buckets"]) == 30
    year = (await repo.get_history(365))["series"]
    assert year["unit"] == "month" and len(year["buckets"]) == 13  # Nov 2025 .. Nov 2026
    assert year["buckets"][0]["key"] == "2025-11" and year["buckets"][0]["partial"] is True   # period starts Nov 7
    assert year["buckets"][-1]["key"] == "2026-11" and year["buckets"][-1]["partial"] is True
    assert not any(b["partial"] for b in year["buckets"][1:-1])
    assert year["buckets"][-1]["episodes"] == 6


@pytest.mark.asyncio
async def test_the_year_can_start_on_the_first(repo, monkeypatch):
    set_now(monkeypatch, 2026, 10, 31, 12)  # 365 days back from Oct 31 2026 is Nov 2 2025
    year = (await repo.get_history(365))["series"]["buckets"]
    assert year[0]["key"] == "2025-11" and year[-1]["key"] == "2026-10" and year[-1]["partial"] is False
    set_now(monkeypatch, 2027, 1, 1, 12)  # first day Jan 2 2026
    assert [b["key"] for b in (await repo.get_history(365))["series"]["buckets"]][0] == "2026-01"


@pytest.mark.asyncio
async def test_empty_library_and_cache(repo):
    h = await repo.get_history(30)
    assert h["totals"]["current"] == {"hours": 0.0, "episodes": 0, "movies": 0, "titles_started": 0,
                                      "titles_finished": 0, "active_days": 0}
    assert h["top_shows"] == [] and h["genres"] == [] and h["weekdays"] == [0] * 7 and h["hours_of_day"] == [0] * 24
    assert [s["service_id"] for s in h["by_service"]] == ["netflix"]  # the active paid service, nothing watched
    assert h["bulk_episodes"] == 0
    # reused until the next write; a caller changing its copy does not change the next
    h["totals"]["current"]["hours"] = 99
    assert (await repo.get_history(30))["totals"]["current"]["hours"] == 0.0
    await add_show(repo, "Late", ["2026-11-05T20:00:00Z"])
    assert (await repo.get_history(30))["totals"]["current"]["episodes"] == 1


@pytest.mark.asyncio
async def test_api_route(setup_test_app):
    repo = setup_test_app
    await add_show(repo, "Api", ["2026-11-05T20:00:00Z"], service=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/history?days=7")
        assert res.status_code == 200 and res.json()["totals"]["current"]["episodes"] == 1
        assert (await ac.get("/api/history")).json()["period"]["days"] == 30  # the default
        assert (await ac.get("/api/history?days=365&include_bulk=true")).json()["include_bulk"] is True
        for bad in ("0", "8", "31", "-7", "abc", "366", ""):
            assert (await ac.get(f"/api/history?days={bad}")).status_code == 422, bad
        assert (await ac.get("/api/history?include_bulk=maybe")).status_code == 422
