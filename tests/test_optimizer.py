"""Tests for Optimizer Engine."""

import pytest
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show, ServiceConfig, ShowStatus, Priority
from penguwatch.config import OptimizerConfig
from penguwatch.optimizer.engine import SubscriptionOptimizer


@pytest.mark.asyncio
async def test_optimizer_recommendations_and_rotation(tmp_path):
    db_file = str(tmp_path / "test_opt.db")
    repo = DatabaseRepository(db_file)
    default_services = [
        ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, is_active=True),
        ServiceConfig(id="netflix", name="Netflix", monthly_cost=15.49, is_active=False),
        ServiceConfig(id="max", name="Max", monthly_cost=16.99, is_active=False),
    ]
    await repo.initialize(default_services)

    # Show on Apple TV+ (Active, in progress)
    await repo.upsert_show(
        Show(
            title="Slow Horses",
            network="Apple TV+",
            service_id="apple_tv",
            status=ShowStatus.WATCHING,
            priority=Priority.HIGH,
            household=["person1", "person2"],
            imdb_rating=8.3,
            total_episodes=24,
            aired_episodes=24,
            watched_episodes=12,
            is_ended=False,
        )
    )

    # Show on Max (Inactive, high priority, ready to binge)
    await repo.upsert_show(
        Show(
            title="The Last of Us",
            network="Max",
            service_id="max",
            status=ShowStatus.PLANTOWATCH,
            priority=Priority.HIGH,
            household=["person1", "person2"],
            imdb_rating=8.8,
            total_episodes=9,
            aired_episodes=9,
            watched_episodes=0,
            is_ended=True,
        )
    )

    optimizer = SubscriptionOptimizer(repository=repo, config=OptimizerConfig())
    plan = await optimizer.calculate_plan()

    assert plan.current_monthly_spend == 9.99
    assert plan.all_services_spend == round(9.99 + 15.49 + 16.99, 2)
    assert plan.monthly_savings > 0
    assert plan.annual_savings == round(plan.monthly_savings * 12.0, 2)

    # Check that Apple TV+ is marked "keep_active" because it has 12 unwatched episodes
    apple_rec = next(r for r in plan.service_rankings if r.service_id == "apple_tv")
    assert apple_rec.is_active is True
    assert apple_rec.action == "keep_active"

    # Check that Max has high binge score and is recommended next
    max_rec = next(r for r in plan.service_rankings if r.service_id == "max")
    assert max_rec.ready_to_binge_count == 1
    assert max_rec.action == "activate_now"

    # Check rotation advice
    assert len(plan.rotation_advice) > 0

    await repo.close()
