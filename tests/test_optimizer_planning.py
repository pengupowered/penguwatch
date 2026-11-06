"""Optimizer: plannable hours, free services, unpriced bundles and unassigned titles."""

import pytest

from penguwatch.config import OptimizerConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import HouseholdTag, Priority, ServiceConfig, Show, ShowStatus
from penguwatch.optimizer.engine import (
    PRIORITY_OR_BOTH_WEIGHT,
    STATUS_WEIGHT,
    SubscriptionOptimizer,
    plannable_hours,
)


def _show(status=ShowStatus.PLANTOWATCH, left=10, runtime=60, service="a", title="T", **kw):
    return Show(title=title, service_id=service, status=status, total_episodes=left, aired_episodes=left,
                watched_episodes=0, runtime_minutes=runtime, **kw)


async def _plan(tmp_path, services, shows):
    repo = DatabaseRepository(str(tmp_path / "planning.db"))
    await repo.initialize(services)
    for s in shows:
        await repo.upsert_show(s)
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    await repo.close()
    return plan


def _rec(plan, sid):
    return next(r for r in plan.service_rankings if r.service_id == sid)


def test_weights_by_status():
    assert STATUS_WEIGHT == {"watching": 1.0, "plantowatch": 0.5, "hold": 0.25}
    assert PRIORITY_OR_BOTH_WEIGHT == 1.0
    assert plannable_hours(_show(ShowStatus.WATCHING)) == 10.0
    assert plannable_hours(_show(ShowStatus.PLANTOWATCH)) == 5.0
    assert plannable_hours(_show(ShowStatus.HOLD)) == 2.5
    assert plannable_hours(_show(ShowStatus.COMPLETED)) == 0.0
    assert plannable_hours(_show(ShowStatus.DROPPED)) == 0.0


def test_high_priority_and_both_count_in_full():
    assert plannable_hours(_show(ShowStatus.HOLD, priority=Priority.HIGH)) == 10.0
    assert plannable_hours(_show(ShowStatus.PLANTOWATCH, household_tag=HouseholdTag.BOTH)) == 10.0


@pytest.mark.asyncio
async def test_bundle_sums_members_and_counts_a_show_once(tmp_path):
    services = [
        ServiceConfig(id="a", name="A", monthly_cost=5.0),
        ServiceConfig(id="b", name="B", monthly_cost=6.0),
        ServiceConfig(id="bun", name="Bundle", monthly_cost=9.0, includes=["a", "b"]),
    ]
    shows = [_show(service="a", title="One"), _show(service="b", title="Two", status=ShowStatus.WATCHING)]
    plan = await _plan(tmp_path, services, shows)
    assert _rec(plan, "a").plannable_hours == 5.0
    assert _rec(plan, "b").plannable_hours == 10.0
    assert _rec(plan, "bun").plannable_hours == 15.0
    assert _rec(plan, "bun").hours_backlog == 20.0


@pytest.mark.asyncio
async def test_free_service_is_never_unused_or_cancelled(tmp_path):
    services = [ServiceConfig(id="free", name="Free TV", monthly_cost=0.0, is_active=True),
                ServiceConfig(id="paid", name="Paid", monthly_cost=8.0, is_active=True)]
    plan = await _plan(tmp_path, services, [])
    free, paid = _rec(plan, "free"), _rec(plan, "paid")
    assert free.is_free and not free.needs_price
    assert free.verdict is None and free.action == "keep_active"
    assert paid.verdict == "unused" and paid.action == "cancel_recommended" and not paid.is_free
    assert plan.unused_monthly_cost == 8.0
    assert not any("Free TV" in line and ("Unused" in line or "Cancel now" in line) for line in plan.rotation_advice)


@pytest.mark.asyncio
async def test_zero_price_bundle_needs_a_price(tmp_path):
    services = [
        ServiceConfig(id="a", name="A", monthly_cost=5.0, is_active=True),
        ServiceConfig(id="b", name="B", monthly_cost=6.0, is_active=True),
        ServiceConfig(id="bun", name="Mystery bundle", monthly_cost=0.0, includes=["a", "b"]),
    ]
    plan = await _plan(tmp_path, services, [_show(service="bun", title="X")])
    bun = _rec(plan, "bun")
    assert bun.needs_price and not bun.is_free and bun.is_bundle
    assert "Set a price for Mystery bundle" in plan.rotation_advice
    assert plan.bundle_suggestions == []
    assert bun.action != "activate_now"
    assert not any("Next in Rotation" in line and "Mystery" in line for line in plan.rotation_advice)


@pytest.mark.asyncio
async def test_covered_paid_member_is_not_free(tmp_path):
    services = [
        ServiceConfig(id="a", name="A", monthly_cost=5.0),
        ServiceConfig(id="bun", name="Bundle", monthly_cost=9.0, is_active=True, includes=["a"]),
    ]
    plan = await _plan(tmp_path, services, [])
    a = _rec(plan, "a")
    assert a.monthly_cost == 0.0 and a.covered_by == "bun" and not a.is_free


@pytest.mark.asyncio
async def test_unassigned_titles_counts_open_shows_without_service(tmp_path):
    services = [ServiceConfig(id="a", name="A", monthly_cost=5.0)]
    shows = [_show(service=None, title="Loose"), _show(service=None, title="Done", status=ShowStatus.COMPLETED),
             _show(service=None, title="Gone", status=ShowStatus.DROPPED), _show(service="a", title="Placed")]
    plan = await _plan(tmp_path, services, shows)
    assert plan.unassigned_titles == 1


@pytest.mark.asyncio
async def test_long_plan_to_watch_runner_no_longer_beats_a_ready_show(tmp_path):
    # 110 h plan-to-watch (raw backlog capped to 60, weighted 55) vs a 58 h watching show. Both are
    # ready to binge, so only the capped backlog term differs: raw hours ranked "long" first (60 > 58).
    services = [ServiceConfig(id="long", name="Long", monthly_cost=5.0),
                ServiceConfig(id="ready", name="Ready", monthly_cost=5.0)]
    shows = [_show(service="long", left=110, title="Runner"),
             _show(service="ready", left=58, title="Ready", status=ShowStatus.WATCHING)]
    plan = await _plan(tmp_path, services, shows)
    long, ready = _rec(plan, "long"), _rec(plan, "ready")
    assert long.hours_backlog == 110.0 and long.plannable_hours == 55.0
    assert ready.plannable_hours == 58.0
    assert ready.binge_score > long.binge_score
    assert [r.service_id for r in plan.service_rankings] == ["ready", "long"]
