"""Optimizer: bundle verdicts, covered members and switching advice."""

from datetime import datetime, timedelta, timezone

import pytest

from penguwatch.config import OptimizerConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig, Show, ShowStatus
from penguwatch.optimizer import bundles
from penguwatch.optimizer.engine import SubscriptionOptimizer


def _services(hulu=True, disney=True, bundle=False, bundle_cost=21.99):
    return [
        ServiceConfig(id="hulu", name="Hulu", monthly_cost=9.99, is_active=hulu),
        ServiceConfig(id="disney", name="Disney+", monthly_cost=13.99, is_active=disney),
        ServiceConfig(id="bundle", name="Disney+ & Hulu bundle (no ads)", monthly_cost=bundle_cost,
                      is_active=bundle, includes=["disney", "hulu"]),
    ]


async def _plan(tmp_path, services, watched=()):
    """watched: service ids that get 5 recent one-hour episodes."""
    repo = DatabaseRepository(str(tmp_path / "bundles.db"))
    await repo.initialize(services)
    db = await repo.get_connection()
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sid in watched:
        show_id = await repo.upsert_show(Show(title=f"Show on {sid}", service_id=sid, status=ShowStatus.WATCHING,
                                              total_episodes=20, aired_episodes=20, watched_episodes=5,
                                              runtime_minutes=60))
        for n in range(1, 6):
            await db.execute("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 1, ?, 1, ?)",
                             (show_id, n, recent))
    await db.commit()
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    await repo.close()
    return plan


def _rec(plan, sid):
    return next(r for r in plan.service_rankings if r.service_id == sid)


@pytest.mark.asyncio
async def test_switch_to_bundle(tmp_path):
    plan = await _plan(tmp_path, _services(), watched=["hulu"])
    assert [s.kind for s in plan.bundle_suggestions] == ["switch_to_bundle"]
    s = plan.bundle_suggestions[0]
    assert s.bundle_id == "bundle" and s.member_ids == ["disney", "hulu"]
    assert s.current_cost == 23.98 and s.bundle_cost == 21.99 and s.monthly_saving == 1.99
    assert any(line == f"Bundle: {s.text}" for line in plan.rotation_advice)
    # the bundle is not offered as "next in rotation" or activate_now: everything is already held
    assert not any("Next in Rotation" in line for line in plan.rotation_advice)
    assert _rec(plan, "bundle").action != "activate_now"
    assert plan.current_monthly_spend == 23.98


@pytest.mark.asyncio
async def test_no_switch_when_not_strictly_cheaper(tmp_path):
    plan = await _plan(tmp_path, _services(bundle_cost=23.98))
    assert plan.bundle_suggestions == []


@pytest.mark.asyncio
async def test_no_switch_when_a_member_is_inactive(tmp_path):
    plan = await _plan(tmp_path, _services(hulu=False))
    assert plan.bundle_suggestions == []


@pytest.mark.asyncio
async def test_bundle_not_worth_it_when_only_one_member_watched(tmp_path):
    plan = await _plan(tmp_path, _services(hulu=False, disney=False, bundle=True), watched=["disney"])
    assert [s.kind for s in plan.bundle_suggestions] == ["bundle_not_worth_it"]
    s = plan.bundle_suggestions[0]
    assert s.member_ids == ["disney"] and s.current_cost == 21.99 and s.monthly_saving == 8.0
    assert "Only Disney+ was watched" in s.text
    assert plan.rotation_advice[-1] == f"Bundle: {s.text}"


@pytest.mark.asyncio
async def test_active_bundle_both_watched_has_no_suggestion(tmp_path):
    plan = await _plan(tmp_path, _services(hulu=False, disney=False, bundle=True), watched=["disney", "hulu"])
    assert plan.bundle_suggestions == []
    assert _rec(plan, "bundle").verdict in ("in_use", "finish_before_renewal", "long_tail")
    assert _rec(plan, "bundle").hours_watched_window == 10.0


@pytest.mark.asyncio
async def test_unwatched_active_bundle_is_unused_and_members_covered(tmp_path):
    plan = await _plan(tmp_path, _services(hulu=False, disney=False, bundle=True))
    bundle = _rec(plan, "bundle")
    assert bundle.is_bundle and bundle.includes == ["disney", "hulu"]
    assert bundle.verdict == "unused" and "21.99" in bundle.verdict_text
    for sid in ("disney", "hulu"):
        r = _rec(plan, sid)
        assert r.covered_by == "bundle" and r.is_active and r.monthly_cost == 0.0
        assert r.verdict == "covered" and r.verdict_text == "Included in Disney+ & Hulu bundle (no ads)"
        assert r.cost_per_hour is None and r.action == "keep_active"
    assert plan.unused_monthly_cost == 21.99
    assert plan.bundle_suggestions == []
    assert sorted(s.id for s in plan.active_services) == ["bundle", "disney", "hulu"]


@pytest.mark.asyncio
async def test_spend_matches_pure_function_and_savings_unchanged(tmp_path):
    services = _services(hulu=True, disney=False, bundle=True)
    services.append(ServiceConfig(id="other", name="Other", monthly_cost=7.0, is_active=False))
    plan = await _plan(tmp_path, services)
    assert plan.current_monthly_spend == bundles.monthly_spend(services) == 21.99
    assert plan.all_services_spend == round(9.99 + 13.99 + 21.99 + 7.0, 2)
    assert plan.monthly_savings == round(plan.all_services_spend - plan.current_monthly_spend, 2)
    assert plan.annual_savings == round(plan.monthly_savings * 12.0, 2)


@pytest.mark.asyncio
async def test_zero_priced_member_does_not_crash(tmp_path):
    services = _services()
    services[0].monthly_cost = 0.0
    plan = await _plan(tmp_path, services, watched=["hulu"])
    assert plan.bundle_suggestions == []  # 13.99 < 21.99: no saving
