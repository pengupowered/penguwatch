"""Billing periods and the one-status-per-service logic (optimizer/status.py, planner rules, migration 0005)."""

from datetime import date, timedelta

import aiosqlite

from penguwatch import clock
from penguwatch.config import OptimizerConfig
from penguwatch.db.migrations import run_migrations
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import OptimizerPlan, OptimizerRecommendation, ServiceConfig, Show, ShowStatus
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.optimizer.planner import build_rotation
from penguwatch.optimizer.renewal import next_renewal
from penguwatch.optimizer.status import apply_status

TODAY = date(2026, 10, 10)


def svc(sid="s", cost=10.0, active=True, **kw):
    return ServiceConfig(id=sid, name=sid.title(), monthly_cost=cost, is_active=active, **kw)


def rec_for(s, hours=0.0, verdict=None, renew_in=None, **kw):
    renewal = (TODAY + timedelta(days=renew_in)).isoformat() if renew_in is not None else None
    base = dict(
        service_id=s.id, service_name=s.name, is_active=s.is_active, monthly_cost=s.monthly_cost,
        binge_score=10.0, hours_backlog=5.0, episodes_backlog=5, shows_count=1, ready_to_binge_count=1,
        shared_count=0, recommendation_reason="", action="keep_active", plannable_hours=5.0,
        hours_watched_window=hours, verdict=verdict, next_renewal=renewal, renewal_in_days=renew_in,
        is_free=not s.monthly_cost and not s.is_bundle,
    )
    base.update(kw)
    return OptimizerRecommendation(**base)


def show(title, sid="s", status=ShowStatus.WATCHING, aired=10, watched=0):
    return Show(title=title, service_id=sid, status=status, total_episodes=aired, aired_episodes=aired,
                watched_episodes=watched, runtime_minutes=60)


def run(services, recs, shows=()):
    return apply_status(recs, services, list(shows), TODAY, 90, 0.5)


# --- status table ---

def test_shogun_case_is_watching_not_cancellable():
    s = svc("hulu", 10.0)
    r = rec_for(s, hours=0.0, verdict="unused", renew_in=12)
    savable, _ = run([s], [r], [show("Shogun", "hulu")])
    assert r.status == "watching" and r.watching_titles == ["Shogun"]
    assert "Shogun is in your Watching list" in r.status_text
    assert r.savable_monthly_cost == 0.0 and savable == 0.0


def test_unused_monthly_is_consider_cancel_with_savings():
    s = svc("idle", 8.0)
    r = rec_for(s, verdict="unused", renew_in=5)
    savable, nxt = run([s], [r], [show("Old", "idle", ShowStatus.COMPLETED, 5, 5)])
    assert r.status == "consider_cancel" and r.savable_monthly_cost == 8.0 and savable == 8.0
    assert "cancelling saves $8.00/mo" in r.status_text
    assert nxt and nxt["service_id"] == "idle" and nxt["kind"] == "renewal" and nxt["days"] == 5


def test_no_titles_assigned_is_check():
    s = svc("bare", 8.0)
    r = rec_for(s, verdict="unused")
    run([s], [r], [])
    assert r.status == "check" and r.savable_monthly_cost == 0.0
    assert "assign titles in Tidy" in r.status_text


def test_in_use_when_hours_watched():
    s = svc("busy", 8.0)
    r = rec_for(s, hours=4.0, verdict="in_use", renew_in=3)
    run([s], [r], [show("A", "busy", ShowStatus.PLANTOWATCH)])
    assert r.status == "in_use" and r.savable_monthly_cost == 0.0


def test_yearly_far_from_renewal_is_paid_through():
    s = svc("y", 5.0, billing_period="yearly", renewal_date=(TODAY + timedelta(days=200)).isoformat())
    r = rec_for(s, verdict="unused", renew_in=200)
    savable, nxt = run([s], [r], [show("A", "y", ShowStatus.PLANTOWATCH)])
    assert r.status == "paid_through" and savable == 0.0 and r.savable_monthly_cost == 0.0
    assert r.decide_by == (TODAY + timedelta(days=170)).isoformat()
    assert "nothing to cancel until then" in r.status_text
    assert nxt is None  # decide_by is 170 days away


def test_yearly_close_to_renewal_and_unused_is_decide_soon():
    s = svc("y", 5.0, billing_period="yearly", renewal_date=(TODAY + timedelta(days=20)).isoformat())
    r = rec_for(s, verdict="unused", renew_in=20)
    savable, nxt = run([s], [r], [show("A", "y", ShowStatus.PLANTOWATCH)])
    assert r.status == "decide_soon" and r.savable_monthly_cost == 5.0 and savable == 5.0
    assert r.decide_by == r.next_renewal
    assert nxt and nxt["kind"] == "decide" and nxt["days"] == 20


def test_yearly_close_to_renewal_but_used_is_in_use():
    s = svc("y", 5.0, billing_period="yearly", renewal_date=(TODAY + timedelta(days=20)).isoformat())
    r = rec_for(s, hours=3.0, renew_in=20)
    run([s], [r], [])
    assert r.status == "in_use" and r.savable_monthly_cost == 0.0


def test_yearly_unknown_date_is_check():
    s = svc("y", 5.0, billing_period="yearly")
    r = rec_for(s, verdict="unused")
    run([s], [r], [show("A", "y")])
    assert r.status == "check" and r.savable_monthly_cost == 0.0
    assert "renewal date" in r.status_text


def test_included_with_and_without_note():
    s = svc("xf", 0.0, notes="included with the phone plan")
    r = rec_for(s)
    run([s], [r], [])
    assert r.status == "included" and r.status_text == "Included, no extra cost: included with the phone plan"
    assert r.savable_monthly_cost == 0.0
    s2 = svc("plain", 0.0)
    r2 = rec_for(s2)
    run([s2], [r2], [])
    assert r2.status_text == "Included, no extra cost"


def test_inactive_and_covered():
    off = svc("off", 9.0, active=False)
    r_off = rec_for(off)
    member = svc("m", 5.0)
    r_m = rec_for(member, covered_by="b", verdict="covered", verdict_text="Included in B")
    run([off, member], [r_off, r_m], [])
    assert r_off.status == "inactive" and r_m.status == "covered"


def test_bundle_uses_members_watching_titles():
    member = svc("hulu", 8.0)
    bundle = svc("disney_bundle", 20.0, includes=["hulu"])
    r = rec_for(bundle, verdict="unused", renew_in=9, is_bundle=True)
    run([member, bundle], [r], [show("Shogun", "hulu")])
    assert r.status == "watching" and r.watching_titles == ["Shogun"]


def test_watching_titles_need_aired_episodes_and_are_capped():
    s = svc("s", 8.0)
    r = rec_for(s, verdict="unused")
    shows = [show(f"T{i}", "s") for i in range(5)] + [show("Caught up", "s", aired=4, watched=4)]
    run([s], [r], shows)
    assert r.watching_titles == ["T0", "T1", "T2"]


def test_status_none_planner_behaviour_unchanged():
    """A hand-built plan without status keeps the verdict-driven rules."""
    r = rec_for(svc("idle", 8.0), verdict="unused")
    assert r.status is None
    out = build_rotation(_plan([r]), TODAY)
    assert _segs(out, "idle")[0] == (0, 0, "cancel")


# --- planner with statuses ---

def _plan(recs, slots=1):
    return OptimizerPlan(
        current_monthly_spend=0.0, all_services_spend=0.0, monthly_savings=0.0, annual_savings=0.0,
        max_active_services=slots, active_services=[], service_rankings=recs, rotation_advice=[],
        household_hours_per_day=1.0,
    )


def _segs(result, sid):
    s = next(x for x in result.services if x.service_id == sid)
    return [(g.start_month, g.end_month, g.kind) for g in s.segments]


def test_planner_never_cancels_watching_even_with_unused_verdict():
    r = rec_for(svc("hulu"), verdict="unused", renew_in=5, status="watching", watching_titles=["Shogun"])
    out = build_rotation(_plan([r]), TODAY)
    assert _segs(out, "hulu")[0][2] == "keep"
    assert out.summary.cancel_now == []


def test_planner_cancels_only_consider_cancel_and_decide_soon_now():
    a = rec_for(svc("a"), verdict="unused", renew_in=5, status="consider_cancel")
    b = rec_for(svc("b"), verdict="unused", renew_in=5, status="decide_soon")
    c = rec_for(svc("c"), verdict="unused", renew_in=5, status="check")
    out = build_rotation(_plan([a, b, c], slots=3), TODAY)
    assert set(out.summary.cancel_now) == {"a", "b"}


def test_planner_holds_paid_through_until_renewal_month():
    r = rec_for(svc("y", billing_period="yearly"), hours=2.0, verdict="in_use", renew_in=200, status="paid_through")
    out = build_rotation(_plan([r]), TODAY)
    assert _segs(out, "y")[0] == (0, 5, "keep")
    assert out.summary.cancel_now == []


def test_planner_paid_through_cancels_in_renewal_month_when_unused():
    r = rec_for(svc("y", billing_period="yearly"), verdict="unused", renew_in=95, status="paid_through")
    out = build_rotation(_plan([r]), TODAY)
    assert _segs(out, "y") == [(0, 2, "keep"), (3, 3, "cancel"), (4, 5, "pause")]


def test_over_slot_rule_never_cancels_something_in_use():
    top = rec_for(svc("top"), hours=9.0, binge_score=99.0, status="in_use")
    w = rec_for(svc("w"), binge_score=1.0, status="watching", watching_titles=["X"], hours=1.0, renew_in=4)
    p = rec_for(svc("p", billing_period="yearly"), binge_score=0.5, status="paid_through", renew_in=100, hours=1.0)
    other = rec_for(svc("o"), hours=2.0, binge_score=0.1, status="in_use", renew_in=4)
    out = build_rotation(_plan([top, w, p, other], slots=1), TODAY)
    assert _segs(out, "w")[0][2] == "keep" and _segs(out, "p")[0][2] == "keep"
    # being above the slot target must not produce a "cancel now" for a service that is in use
    assert _segs(out, "o")[0][2] == "keep"
    assert out.summary.cancel_now == []
    assert any("above your target of 1" in n and "o" in n for n in out.notes)


# --- renewal ---

def test_next_renewal_yearly_rolls_by_years_and_ignores_day():
    y = ServiceConfig(id="y", name="Y", billing_period="yearly", renewal_date="2024-03-15", renewal_day=3)
    assert next_renewal(y, date(2026, 3, 15)) == date(2026, 3, 15)
    assert next_renewal(y, date(2026, 3, 16)) == date(2027, 3, 15)
    assert next_renewal(ServiceConfig(id="n", name="N", billing_period="yearly", renewal_day=5), TODAY) is None


def test_next_renewal_yearly_leap_day():
    y = ServiceConfig(id="y", name="Y", billing_period="yearly", renewal_date="2024-02-29")
    assert next_renewal(y, date(2025, 1, 1)) == date(2025, 2, 28)
    assert next_renewal(y, date(2027, 6, 1)) == date(2028, 2, 29)


# --- storage ---

async def test_billing_period_round_trips_and_seeding_keeps_it(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "b.db"))
    await repo.initialize([svc("net", 12.0)])
    await repo.upsert_service(ServiceConfig(id="net", name="Net", monthly_cost=1.5, is_active=True,
                                            billing_period="yearly", renewal_date="2027-01-01"))
    got = await repo.get_service("net")
    assert got.billing_period == "yearly" and got.renewal_date == "2027-01-01"
    await repo.initialize([svc("net", 12.0)])  # seeding again at start-up
    assert (await repo.get_service("net")).billing_period == "yearly"
    await repo.close()


async def test_migration_0005_fresh_and_legacy():
    async with aiosqlite.connect(":memory:") as conn:
        await run_migrations(conn)
        cur = await conn.execute("PRAGMA table_info(services)")
        assert "billing_period" in {r[1] for r in await cur.fetchall()}
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("CREATE TABLE services (id TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_cost REAL "
                           "DEFAULT 0.0, is_active INTEGER DEFAULT 0, renewal_day INTEGER, renewal_date TEXT, "
                           "aliases_json TEXT DEFAULT '[]', notes TEXT)")
        await conn.execute("INSERT INTO services (id, name) VALUES ('old', 'Old')")
        await conn.commit()
        await run_migrations(conn)
        cur = await conn.execute("SELECT billing_period FROM services WHERE id = 'old'")
        assert (await cur.fetchone())[0] == "monthly"


# --- end to end ---

async def test_plan_end_to_end_yearly_and_watching(tmp_path):
    today = clock.today()
    repo = DatabaseRepository(str(tmp_path / "e.db"))
    await repo.initialize([
        svc("hulu", 10.0, renewal_day=min(28, today.day)),
        svc("yr", 5.0),
        svc("idle", 8.0),
    ])
    await repo.upsert_service(ServiceConfig(
        id="yr", name="Yr", monthly_cost=5.0, is_active=True, billing_period="yearly",
        renewal_date=(today + timedelta(days=200)).isoformat(),
    ))
    await repo.upsert_show(show("Shogun", "hulu"))
    await repo.upsert_show(show("Done", "idle", ShowStatus.COMPLETED, 3, 3))
    await repo.upsert_show(show("Waiting", "yr", ShowStatus.PLANTOWATCH))
    plan = await SubscriptionOptimizer(repository=repo, config=OptimizerConfig()).calculate_plan()
    by = {r.service_id: r for r in plan.service_rankings}
    assert by["hulu"].status == "watching" and by["hulu"].verdict == "unused"
    assert by["yr"].status == "paid_through" and by["yr"].billing_period == "yearly"
    assert by["idle"].status == "consider_cancel"
    assert plan.savable_monthly_cost == 8.0
    assert plan.unused_monthly_cost == 23.0  # legacy field untouched: hulu, idle and yr have no viewing
    await repo.close()


def test_planner_never_cancels_a_check_service():
    a = rec_for(svc("a"), binge_score=9.0, hours=2.0, status="in_use")
    no_titles = rec_for(svc("c1"), plannable_hours=0.0, binge_score=1.0, status="check", renew_in=4)
    over_slot = rec_for(svc("c2"), binge_score=0.1, status="check", renew_in=4, verdict="unused")
    out = build_rotation(_plan([a, no_titles, over_slot], slots=1), TODAY)
    assert "cancel" not in {k for sid in ("c1", "c2") for _, _, k in _segs(out, sid)[:1]}
    assert out.summary.cancel_now == []


def test_a_check_service_inside_the_slots_gets_no_cancel_marker_anywhere():
    # e.g. HIDIVE: no titles assigned yet, so no use is visible. It must stay "keep" for the whole horizon.
    c = rec_for(svc("hidive"), plannable_hours=5.0, binge_score=3.0, status="check", renew_in=20)
    out = build_rotation(_plan([c], slots=3), TODAY)
    kinds = {k for _, _, k in _segs(out, "hidive")}
    assert kinds == {"keep"}
    assert out.summary.cancel_now == []
