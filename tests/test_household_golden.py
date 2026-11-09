"""With two people everything scores as before 0.17. The same library as the old model built it (two members, titles
tagged person1, person2 or both; "both" is now {person1, person2}) gives exactly the optimizer scores, the plan and the
rotation, the interest values and reasons, Tonight's Pick weights and the order of the recommendation seeds that
a2f792e (0.17 before the household change) gave. The numbers below were computed by running a2f792e's code on this
library; they are pinned, never recomputed by the code under test."""

from datetime import date

import pytest

from penguwatch import pick
from penguwatch.api.discover import recommendation_seeds
from penguwatch.config import OptimizerConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.interest import build_profile, score
from penguwatch.models import Priority, ServiceConfig, Show, ShowStatus
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.optimizer.planner import build_rotation

WHO = {"p1": ["person1"], "p2": ["person2"], "both": ["person1", "person2"]}
# (title, service, who, status, priority, imdb, votes, total, aired, watched, ended, genres, user_rating, simkl id)
LIBRARY = [
    ("Alpha", "svc_a", "both", "watching", "high", 8.6, 120000, 20, 20, 5, True, ["Drama"], None, 101),
    ("Bravo", "svc_a", "p1", "plantowatch", "medium", 7.4, 40000, 10, 10, 0, True, ["Comedy"], None, 102),
    ("Charlie", "svc_a", "p2", "hold", "low", 6.9, 9000, 30, 24, 6, False, ["Drama", "Crime"], None, 103),
    ("Delta", "svc_b", "both", "plantowatch", "medium", 8.1, 300000, 8, 8, 0, True, ["Sci-Fi"], None, 104),
    ("Echo", "svc_b", "both", "plantowatch", "low", 7.0, 2000, 12, 6, 0, False, ["Comedy"], None, 105),
    ("Foxtrot", "svc_b", "p1", "watching", "high", 8.9, 800000, 40, 40, 39, True, ["Drama"], 9.0, 106),
    ("Golf", "svc_c", "p2", "plantowatch", "high", 7.8, 15000, 16, 16, 0, True, ["Sci-Fi", "Drama"], None, 107),
    ("Hotel", "svc_c", "both", "completed", "medium", 8.4, 50000, 10, 10, 10, True, ["Crime"], 8.5, 108),
    ("India", "svc_c", "p1", "plantowatch", "medium", None, None, 6, 6, 0, True, ["Documentary"], None, 109),
    ("Juliet", "svc_a", "both", "hold", "medium", 7.7, 60000, 22, 22, 11, True, ["Drama"], None, 110),
    ("Kilo", "svc_b", "p2", "watching", "medium", 8.3, 100000, 18, 18, 2, True, ["Crime", "Drama"], 8.0, 111),
    ("Lima", "svc_c", "p1", "watching", "medium", 8.8, 70000, 10, 10, 3, True, ["Drama"], None, 112),
    ("Mike", "svc_a", "p2", "completed", "low", 8.5, 30000, 8, 8, 8, True, ["Comedy"], None, 113),
]
SERVICES = [
    ServiceConfig(id="svc_a", name="Service A", monthly_cost=10.0, is_active=True),
    ServiceConfig(id="svc_b", name="Service B", monthly_cost=12.0, is_active=False),
    ServiceConfig(id="svc_c", name="Service C", monthly_cost=8.0, is_active=False),
]

# --- a2f792e's answers ------------------------------------------------------------------------------------------------
# [service, binge score, action, titles both want (now: shared), plannable hours, top titles]
RANKINGS = [["svc_a", 154.8, "keep_active", 2, 26.6, ["Alpha", "Juliet", "Bravo"]],
            ["svc_b", 154.8, "activate_now", 2, 23.2, ["Foxtrot", "Delta", "Kilo"]],
            ["svc_c", 97.9, "standby", 0, 19.5, ["Lima", "Golf", "India"]]]
ROTATION = {"picks": {"svc_a": ["Alpha", "Juliet", "Bravo"], "svc_b": ["Foxtrot", "Delta", "Kilo"],
                      "svc_c": ["Lima", "Golf", "India"]},
            "services": [["svc_a", [[0, 2, "keep"], [3, 3, "cancel"], [4, 5, "pause"]]],
                         ["svc_b", [[0, 2, "subscribe"], [3, 3, "cancel"], [4, 5, "pause"]]],
                         ["svc_c", [[0, 2, "subscribe"], [3, 3, "cancel"], [4, 5, "pause"]]]],
            "summary": {"cancel_now": [], "current_monthly_spend": 10.0, "monthly_difference": 5.0,
                        "planned_avg_monthly_spend": 15.0}}
INTEREST = {"Alpha": [83, ["IMDb 8.6 (120K votes)", "Both want it", "High priority"]],
            "Bravo": [48, ["IMDb 7.4 (40K votes)"]], "Charlie": [41, ["IMDb 6.9 (9K votes)"]],
            "Delta": [66, ["IMDb 8.1 (300K votes)", "Both want it"]],
            "Echo": [51, ["IMDb 7.0 (2K votes)", "Both want it"]],
            "Foxtrot": [72, ["IMDb 8.9 (800K votes)", "High priority"]],
            "Golf": [60, ["IMDb 7.8 (15K votes)", "High priority"]],
            "Hotel": [71, ["IMDb 8.4 (50K votes)", "Both want it"]], "India": [42, ["Little rating data"]],
            "Juliet": [66, ["IMDb 7.7 (60K votes)", "Both want it"]], "Kilo": [59, ["IMDb 8.3 (100K votes)"]],
            "Lima": [65, ["IMDb 8.8 (70K votes)"]], "Mike": [52, ["IMDb 8.5 (30K votes)"]]}
PICK_WEIGHTS = {"Alpha": 25.2, "Bravo": 1.6500000000000001, "Charlie": 1.0, "Delta": 5.7375, "Echo": 3.0,
                "Foxtrot": 8.850000000000001, "Golf": 3.5999999999999996, "Hotel": 6.075, "India": 1.5,
                "Juliet": 5.2875000000000005, "Kilo": 3.9750000000000005, "Lima": 4.3500000000000005, "Mike": 1.375}
SEEDS = ["Foxtrot", "Hotel", "Kilo", "Alpha", "Lima", "Mike"]


def show(row) -> Show:
    title, svc, who, status, prio, imdb, votes, total, aired, watched, ended, genres, mine, key = row
    return Show(title=title, service_id=svc, household=WHO[who], status=ShowStatus(status), priority=Priority(prio),
                imdb_rating=imdb, imdb_votes=votes, total_episodes=total, aired_episodes=aired,
                watched_episodes=watched, is_ended=ended, genres=genres, user_rating=mine, runtime_minutes=45,
                ids={"simkl": key})


@pytest.fixture
async def library(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "golden.db"))
    await repo.initialize(SERVICES)
    repo.set_household_members(["person1", "person2"])  # as the settings component does
    for row in LIBRARY:
        await repo.upsert_show(show(row))
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig(), value_window_days=90).calculate_plan()
    shows = await repo.get_shows()
    await repo.close()
    return plan, shows


async def test_the_optimizer_scores_and_plan_are_unchanged(library):
    plan, _ = library
    got = [[r.service_id, r.binge_score, r.action, r.shared_count, r.plannable_hours,
            [t["title"] for t in r.top_shows]] for r in plan.service_rankings]
    assert got == RANKINGS


async def test_the_rotation_is_unchanged(library):
    plan, _ = library
    rotation = build_rotation(plan, date(2026, 1, 1), 6)
    got = {"services": [[s.service_id, [[g.start_month, g.end_month, g.kind] for g in s.segments]]
                        for s in rotation.services],
           "picks": {k: [p.title for p in v] for k, v in rotation.picks.items()},
           "summary": rotation.summary.model_dump()}
    assert got == ROTATION


async def test_the_interest_values_and_reasons_are_unchanged(library):
    _, shows = library
    profile = build_profile(shows)
    assert {s.title: list(score(s, profile, 2)) for s in shows} == INTEREST


async def test_the_pick_weights_are_unchanged(library):
    _, shows = library
    assert {s.title: pick.weight(s) for s in shows} == PICK_WEIGHTS


async def test_the_seed_order_is_unchanged(library):
    _, shows = library
    assert [s.title for s in recommendation_seeds(shows, "simkl")] == SEEDS
