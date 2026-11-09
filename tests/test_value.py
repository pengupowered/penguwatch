"""Optimizer value & pace: cost per hour, finish-before-renewal, unused subscriptions."""

from datetime import date, datetime, timedelta, timezone

import pytest

from penguwatch.config import OptimizerConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show, ServiceConfig, ShowStatus
from penguwatch.optimizer.engine import SubscriptionOptimizer, next_renewal


def test_next_renewal_from_day_and_date():
    svc = ServiceConfig(id="x", name="X", renewal_day=31)
    assert next_renewal(svc, date(2026, 2, 10)) == date(2026, 2, 28)  # clipped to month length
    assert next_renewal(svc, date(2026, 3, 31)) == date(2026, 3, 31)
    dated = ServiceConfig(id="y", name="Y", renewal_date="2026-01-15")
    assert next_renewal(dated, date(2026, 10, 10)) == date(2026, 10, 15)
    assert next_renewal(dated, date(2026, 10, 16)) == date(2026, 11, 15)
    assert next_renewal(ServiceConfig(id="z", name="Z"), date(2026, 1, 1)) is None


@pytest.mark.asyncio
async def test_value_verdicts(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "value.db"))
    renewal_day = (date.today() + timedelta(days=40)).day
    await repo.initialize([
        ServiceConfig(id="used", name="Used+", monthly_cost=10.0, is_active=True, renewal_day=min(28, renewal_day)),
        ServiceConfig(id="idle", name="Idle+", monthly_cost=8.0, is_active=True),
        ServiceConfig(id="off", name="Off+", monthly_cost=12.0, is_active=False),
    ])
    show_id = await repo.upsert_show(Show(title="Watched Lately", service_id="used", status=ShowStatus.WATCHING,
                                          total_episodes=20, aired_episodes=20, watched_episodes=10, runtime_minutes=60))
    await repo.upsert_show(Show(title="Gathering Dust", service_id="idle", status=ShowStatus.PLANTOWATCH,
                                total_episodes=10, aired_episodes=10, watched_episodes=0, runtime_minutes=60))
    db = await repo.get_connection()
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for n in range(1, 11):
        await db.execute("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 1, ?, 1, ?)",
                         (show_id, n, recent))
    await db.commit()

    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    used = next(r for r in plan.service_rankings if r.service_id == "used")
    idle = next(r for r in plan.service_rankings if r.service_id == "idle")
    off = next(r for r in plan.service_rankings if r.service_id == "off")

    assert used.hours_watched_window == 10.0 and used.episodes_watched_window == 10
    assert used.cost_per_hour == round(10.0 * (90 / 30.44) / 10.0, 2)
    assert used.days_to_clear == 90  # 10 h backlog at 10 h per 90 days
    assert used.verdict in ("finish_before_renewal", "in_use")
    assert idle.verdict == "unused" and "Cancelling saves" in idle.verdict_text
    assert off.verdict is None  # inactive services get no cancel verdict
    assert plan.unused_monthly_cost == 8.0
    assert any(a.startswith("Unused:") for a in plan.rotation_advice)
    await repo.close()


@pytest.mark.asyncio
async def test_plan_with_no_services_does_not_crash(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "empty.db"))
    await repo.initialize([])
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    assert plan.service_rankings == []
    await repo.close()


@pytest.mark.asyncio
async def test_progress_delta_is_bounded():
    from pydantic import ValidationError
    from penguwatch.models import ShowProgressUpdate

    with pytest.raises(ValidationError):
        ShowProgressUpdate(episode_delta=10_000_000)
    assert ShowProgressUpdate(episode_delta=3).episode_delta == 3


@pytest.mark.asyncio
async def test_huge_backlog_does_not_dominate_ranking(tmp_path):
    from penguwatch.models import Priority

    repo = DatabaseRepository(str(tmp_path / "cap.db"))
    await repo.initialize([
        ServiceConfig(id="longrunner", name="LongRunner+", monthly_cost=8.0),
        ServiceConfig(id="bingeable", name="Bingeable+", monthly_cost=8.0),
    ])
    # 500 h of one weekly show still airing (not ready to binge)
    await repo.upsert_show(Show(title="Endless", service_id="longrunner", status=ShowStatus.PLANTOWATCH,
                                total_episodes=1000, aired_episodes=1000, watched_episodes=0, runtime_minutes=30,
                                next_episode_air_date="2099-01-01"))
    await repo.upsert_show(Show(title="Limited", service_id="bingeable", status=ShowStatus.PLANTOWATCH,
                                total_episodes=9, aired_episodes=9, watched_episodes=0, runtime_minutes=45,
                                is_ended=True, priority=Priority.HIGH, household=["person1", "person2"], imdb_rating=8.5))
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    scores = {r.service_id: r.binge_score for r in plan.service_rankings}
    assert plan.service_rankings[0].service_id == "bingeable", scores
    await repo.close()
