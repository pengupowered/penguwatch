"""Likely-interest score: profile arithmetic, quality shrinkage, API sort and optimizer top picks."""

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.config import OptimizerConfig
from penguwatch.interest import TasteProfile, build_profile, score
from penguwatch.main import app
from penguwatch.models import HouseholdTag, Priority, Show, ShowStatus
from penguwatch.optimizer.engine import SubscriptionOptimizer


def _library():
    return [
        Show(title="A", genres=["Drama", "Crime"], user_rating=9.0),
        Show(title="B", genres=["Drama"], status=ShowStatus.COMPLETED),
        Show(title="C", genres=["Comedy"], status=ShowStatus.DROPPED),
    ]


def test_profile_sums_signals_per_genre():
    profile = build_profile(_library() + [Show(title="Plan", genres=["Drama"])])
    assert profile.genre_sum["Drama"] == pytest.approx(0.9 + 0.7)
    assert profile.genre_count == {"Drama": 2, "Crime": 1, "Comedy": 1}
    assert profile.genre_sum["Comedy"] == pytest.approx(0.15)


def test_signal_precedence_and_watching():
    shows = [
        Show(title="R", genres=["X"], user_rating=8.0, status=ShowStatus.DROPPED),
        Show(title="W", genres=["X"], status=ShowStatus.WATCHING, watched_episodes=2),
        Show(title="W0", genres=["Y"], status=ShowStatus.WATCHING, watched_episodes=0),
    ]
    profile = build_profile(shows)
    assert profile.genre_sum["X"] == pytest.approx(0.8 + 0.6)
    assert profile.genre_count == {"X": 2}


def test_score_with_affinity_and_quality():
    profile = build_profile(_library())
    target = Show(title="D", genres=["Drama", "Comedy"], imdb_rating=8.0, imdb_votes=1000)
    taste = ((1.6 + 1.5) / 5 + (0.15 + 1.5) / 4) / 2
    quality = ((8.0 * 1000 + 6.5 * 500) / 1500 - 5.0) / 4.0
    expected = round(100 * (0.40 * quality + 0.35 * taste + 0.25 * 0.2))
    value, reasons = score(target, profile)
    assert value == expected == 48
    assert reasons == ["IMDb 8.0 (1K votes)"]


def test_leave_one_out_removes_own_contribution():
    lib = _library()
    profile = build_profile(lib)
    value, reasons = score(lib[0], profile)  # no ratings -> Q=0.5, medium priority
    taste = ((0.7 + 1.5) / 4 + 1.5 / 3) / 2  # Drama and Crime without A itself
    assert value == round(100 * (0.40 * 0.5 + 0.35 * taste + 0.25 * 0.2)) == 43
    assert reasons == ["Little rating data"]


def test_vote_count_shrinks_rating():
    profile = TasteProfile({}, {})
    few = score(Show(title="x", imdb_rating=8.0, imdb_votes=50), profile)[0]
    many = score(Show(title="x", imdb_rating=8.0, imdb_votes=500_000), profile)[0]
    assert few == round(100 * (0.40 * (((400 + 3250) / 550) - 5.0) / 4.0 + 0.35 * 0.5 + 0.25 * 0.2)) == 39
    assert many == 52


def test_missing_ratings_and_simkl_fallback():
    profile = TasteProfile({}, {})
    value, reasons = score(Show(title="x", priority=Priority.LOW), profile)
    assert value == round(100 * (0.40 * 0.5 + 0.35 * 0.5)) == 38
    assert reasons == ["Little rating data"]
    value, reasons = score(Show(title="x", simkl_rating=7.9, priority=Priority.LOW), profile)
    q = (((7.9 * 500 + 6.5 * 500) / 1000) - 5.0) / 4.0
    assert value == round(100 * (0.40 * q + 0.35 * 0.5))
    assert reasons == ["SIMKL 7.9"]


def test_reasons_order_and_vote_formatting():
    genres = ["Drama", "Crime", "Thriller"]
    profile = build_profile([Show(title=f"s{i}", genres=genres, user_rating=9.0) for i in range(5)])
    show = Show(title="t", genres=genres, imdb_rating=8.4, imdb_votes=1_234_567,
                household_tag=HouseholdTag.BOTH, priority=Priority.HIGH)
    reasons = score(show, profile)[1]
    assert reasons == ["IMDb 8.4 (1.2M votes)", "You usually finish: Crime, Drama", "Both want it", "High priority"]
    assert score(Show(title="t", imdb_rating=7.1, imdb_votes=45_000), profile)[1][0] == "IMDb 7.1 (45K votes)"
    assert score(Show(title="t", imdb_rating=6.0, imdb_votes=850), profile)[1][0] == "IMDb 6.0 (850 votes)"
    assert score(Show(title="t", imdb_rating=6.0), profile)[1][0] == "IMDb 6.0"


def test_score_is_clamped_to_0_100():
    profile = TasteProfile({}, {})
    best = Show(title="x", imdb_rating=10.0, imdb_votes=10_000_000, priority=Priority.HIGH,
                household_tag=HouseholdTag.BOTH)
    worst = Show(title="x", imdb_rating=1.0, imdb_votes=10_000_000, priority=Priority.LOW)
    assert 0 <= score(best, profile)[0] <= 100
    assert 0 <= score(worst, profile)[0] <= 100


def test_serializer_omits_unset_fields_and_keeps_schema():
    show = Show(title="x")
    assert "interest_score" not in show.model_dump()
    assert "interest_reasons" not in show.model_dump()
    assert "interest_score" not in show.model_dump_json()
    show.interest_score, show.interest_reasons = 70, ["High priority"]
    dumped = show.model_dump()
    assert dumped["interest_score"] == 70 and dumped["interest_reasons"] == ["High priority"]
    props = app.openapi()["components"]["schemas"]["Show"]["properties"]
    assert "title" in props and "episodes_left" in props and "interest_score" in props


@pytest.fixture
async def repo(container):
    yield container.repository


async def _add(repo, **kw):
    return await repo.upsert_show(Show(**kw))


@pytest.mark.asyncio
async def test_api_sort_by_interest(repo):
    await _add(repo, title="Plain", imdb_rating=6.0, imdb_votes=100000)
    await _add(repo, title="Great", imdb_rating=9.0, imdb_votes=100000, priority=Priority.HIGH)
    # equal score: same adjusted rating through IMDb (500 votes) vs the SIMKL fallback
    await _add(repo, title="Zeta", simkl_rating=7.0)
    await _add(repo, title="Alpha", imdb_rating=7.0, imdb_votes=500)
    await _add(repo, title="Beta", simkl_rating=7.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/shows", params={"sort_by": "interest"})
        assert res.status_code == 200
        body = res.json()
        assert [s["title"] for s in body] == ["Great", "Alpha", "Beta", "Zeta", "Plain"]
        assert all(isinstance(s["interest_score"], int) and s["interest_reasons"] for s in body)
        scores = [s["interest_score"] for s in body]
        assert scores == sorted(scores, reverse=True)
        # filters still apply
        res = await ac.get("/api/shows", params={"sort_by": "interest", "priority": "high"})
        assert [s["title"] for s in res.json()] == ["Great"]
        # other sort options are untouched and carry no interest fields
        for sort_by in ("priority", "title", "rating"):
            res = await ac.get("/api/shows", params={"sort_by": sort_by})
            assert res.status_code == 200
            assert all("interest_score" not in s and "interest_reasons" not in s for s in res.json())
        res = await ac.get("/api/shows")
        assert all("interest_score" not in s for s in res.json())
        one = await ac.get(f"/api/shows/{body[0]['id']}")
        assert "interest_score" not in one.json()


@pytest.mark.asyncio
async def test_optimizer_top_shows_follow_interest_score(container):
    repo = container.repository
    await _add(repo, title="Mediocre but high priority", service_id="netflix", priority=Priority.HIGH,
               imdb_rating=5.0, imdb_votes=100000, total_episodes=10, aired_episodes=10)
    await _add(repo, title="Acclaimed", service_id="netflix", priority=Priority.LOW,
               imdb_rating=9.0, imdb_votes=100000, total_episodes=10, aired_episodes=10)
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    netflix = next(r for r in plan.service_rankings if r.service_id == "netflix")
    assert [t["title"] for t in netflix.top_shows] == ["Acclaimed", "Mediocre but high priority"]
    assert set(netflix.top_shows[0]) == {
        "id", "title", "year", "poster", "imdb_rating", "episodes_left",
        "is_ready_to_binge", "status", "priority", "household_tag",
    }
