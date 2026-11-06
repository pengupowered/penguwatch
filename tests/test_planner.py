"""Pure rotation planner: hand-built plans, no database."""

from datetime import date

from penguwatch.models import OptimizerPlan, OptimizerRecommendation
from penguwatch.optimizer.planner import PACE_FLOOR, _cancel_month, _needed_months, build_rotation

TODAY = date(2026, 10, 10)


def rec(sid, score=50.0, cost=10.0, hours=30.0, ready=1, active=False, **kw):
    base = dict(
        service_id=sid,
        service_name=sid.title(),
        is_active=active,
        monthly_cost=cost,
        binge_score=score,
        hours_backlog=hours,
        episodes_backlog=10,
        shows_count=1,
        ready_to_binge_count=ready,
        both_want_count=0,
        recommendation_reason="",
        action="keep_active" if active else "standby",
        plannable_hours=hours,
    )
    base.update(kw)
    return OptimizerRecommendation(**base)


def plan_of(recs, slots=1, pace=1.0, current=0.0, unassigned=0):
    return OptimizerPlan(
        current_monthly_spend=current,
        all_services_spend=0.0,
        monthly_savings=0.0,
        annual_savings=0.0,
        max_active_services=slots,
        active_services=[],
        service_rankings=recs,
        rotation_advice=[],
        household_hours_per_day=pace,
        unassigned_titles=unassigned,
    )


def segs(result, sid):
    svc = next(s for s in result.services if s.service_id == sid)
    return [(s.start_month, s.end_month, s.kind) for s in svc.segments]


def held_months(result, sid):
    svc = next(s for s in result.services if s.service_id == sid)
    out = set()
    for s in svc.segments:
        if s.kind in ("keep", "subscribe"):
            out.update(range(s.start_month, s.end_month + 1))
    return out


def test_helpers():
    assert _needed_months(rec("a", hours=30), 1.0) == 1
    assert _needed_months(rec("a", hours=31), 1.0) == 2
    assert _needed_months(rec("a", hours=500), 1.0) == 3
    assert _needed_months(rec("a", hours=0.1), 5.0) == 1
    assert _cancel_month(rec("a", renewal_in_days=None)) == 0
    assert _cancel_month(rec("a", renewal_in_days=29)) == 0
    assert _cancel_month(rec("a", renewal_in_days=70)) == 2


def test_one_slot_three_candidates_in_score_order():
    recs = [rec("c", 50, 5, 30), rec("a", 90, 10, 30), rec("b", 80, 8, 60)]
    r = build_rotation(plan_of(recs, current=0.0), TODAY)
    assert segs(r, "a") == [(0, 0, "subscribe"), (1, 1, "cancel"), (2, 5, "pause")]
    assert segs(r, "b") == [(1, 2, "subscribe"), (3, 3, "cancel"), (4, 5, "pause")]
    assert segs(r, "c") == [(3, 3, "subscribe"), (4, 4, "cancel"), (5, 5, "pause")]
    # a 1 month x 10, b 2 x 8, c 1 x 5 over 6 months
    assert r.summary.planned_avg_monthly_spend == 5.17
    assert r.summary.monthly_difference == 5.17
    assert r.summary.cancel_now == []
    assert [s.service_id for s in r.services] == ["c", "a", "b"]
    labels = {s.label for s in r.services[1].segments}
    assert "Subscribe (1 mo)" in labels


def test_unused_paid_service_cancelled_at_renewal_or_now():
    r = build_rotation(
        plan_of([rec("x", active=True, verdict="unused", renewal_in_days=45, hours=0)], current=10.0), TODAY
    )
    assert segs(r, "x") == [(0, 0, "keep"), (1, 1, "cancel"), (2, 5, "pause")]
    assert r.summary.cancel_now == []
    assert r.summary.planned_avg_monthly_spend == 1.67
    assert r.summary.monthly_difference == -8.33

    r = build_rotation(plan_of([rec("y", active=True, verdict="unused", hours=0)], current=10.0), TODAY)
    assert segs(r, "y") == [(0, 0, "cancel"), (1, 5, "pause")]
    assert r.summary.cancel_now == ["y"]
    assert r.summary.planned_avg_monthly_spend == 0.0
    assert any("Check your renewal day for Y" in n for n in r.notes)
    assert next(s for s in r.services if s.service_id == "y").segments[0].label == "Cancel now"


def test_over_the_slot_limit_cancels_the_rest_at_their_renewals():
    recs = [
        rec("x", 90, 10, 30, active=True, renewal_in_days=10),
        rec("y", 50, 20, 30, active=True, renewal_in_days=70),
        rec("z", 40, 5, 30, active=True, renewal_in_days=35),
    ]
    r = build_rotation(plan_of(recs, slots=1, current=35.0), TODAY)
    assert segs(r, "x") == [(0, 0, "keep"), (1, 1, "cancel"), (2, 5, "pause")]
    assert segs(r, "y") == [(0, 1, "keep"), (2, 2, "cancel"), (3, 5, "pause")]
    assert segs(r, "z") == [(0, 0, "keep"), (1, 1, "cancel"), (2, 5, "pause")]
    # x 10 x 1, y 20 x 2, z 5 x 1 over 6 months
    assert r.summary.planned_avg_monthly_spend == 9.17
    assert r.summary.monthly_difference == -25.83


def test_kept_service_with_cleared_backlog_leaves_at_renewal():
    r = build_rotation(plan_of([rec("x", active=True, hours=0, renewal_in_days=65)]), TODAY)
    assert segs(r, "x") == [(0, 1, "keep"), (2, 2, "cancel"), (3, 5, "pause")]


def test_wait_for_finale_is_never_subscribed():
    recs = [rec("w", 99, 10, 30, action="wait_for_finale"), rec("o", 10, 5, 30)]
    r = build_rotation(plan_of(recs), TODAY)
    assert segs(r, "w") == [(0, 5, "wait")]
    assert segs(r, "o")[0] == (0, 0, "subscribe")
    assert "w" not in r.picks


def test_free_service_is_kept_and_outside_the_slots():
    recs = [rec("f", 99, 0.0, 30, is_free=True), rec("p", 50, 10, 30)]
    r = build_rotation(plan_of(recs, slots=1), TODAY)
    assert segs(r, "f") == [(0, 5, "keep")]
    assert r.services[0].segments[0].label == "Free"
    assert segs(r, "p")[0] == (0, 0, "subscribe")
    assert "f" not in r.picks
    assert r.summary.planned_avg_monthly_spend == 1.67


def test_pace_zero_uses_the_floor():
    r = build_rotation(plan_of([rec("a", hours=100)], pace=0.0), TODAY)
    assert r.pace_floor_applied is True
    assert r.pace_hours_per_day == PACE_FLOOR
    # 100 / (0.25 * 30) = 13.3 months, clamped to 3
    assert segs(r, "a")[0] == (0, 2, "subscribe")
    assert any("Pace below" in n for n in r.notes)
    r = build_rotation(plan_of([rec("a")], pace=1.0), TODAY)
    assert r.pace_floor_applied is False


def test_empty_plan_is_an_empty_timeline_with_a_note():
    r = build_rotation(plan_of([], unassigned=4), TODAY, months=3)
    assert r.services == [] and r.picks == {}
    assert r.months == 3
    assert "No services to plan yet." in r.notes
    assert r.summary.planned_avg_monthly_spend == 0.0


def test_active_bundle_keeps_members_and_never_subscribes_them():
    recs = [
        rec("b", 70, 20, 30, active=True, is_bundle=True, includes=["m1", "m2"], renewal_in_days=5),
        rec("m1", 40, 0.0, 10, active=True, covered_by="b"),
        rec("m2", 30, 0.0, 10, active=True, covered_by="b"),
        rec("c", 20, 6, 30),
    ]
    r = build_rotation(plan_of(recs, slots=1, current=20.0), TODAY)
    assert segs(r, "b") == [(0, 0, "keep"), (1, 1, "cancel"), (2, 5, "pause")]
    for m in ("m1", "m2"):
        assert segs(r, m) == [(0, 0, "keep"), (1, 5, "pause")]
        assert r.services[recs.index(next(x for x in recs if x.service_id == m))].segments[0].label == (
            "Included in B"
        )
    assert segs(r, "c")[0] == (1, 1, "subscribe")
    assert r.summary.planned_avg_monthly_spend == 4.33  # (20 + 6) / 6


def test_inactive_bundle_and_members_are_mutually_exclusive():
    recs = [
        rec("b", 80, 20, 30, is_bundle=True, includes=["m1"]),
        rec("m1", 60, 8, 30),
        rec("m2", 10, 5, 30),
    ]
    r = build_rotation(plan_of(recs, slots=2), TODAY)
    assert held_months(r, "b").isdisjoint(held_months(r, "m1"))
    assert segs(r, "b")[0] == (0, 0, "subscribe")
    assert segs(r, "m2")[0] == (0, 0, "subscribe")  # takes the second slot instead
    assert segs(r, "m1")[0] == (1, 1, "subscribe")  # after the bundle left


def test_tie_goes_to_the_cheaper():
    recs = [rec("b", 60, 20, 30, is_bundle=True, includes=["m1"]), rec("m1", 60, 8, 30)]
    r = build_rotation(plan_of(recs, slots=2), TODAY)
    assert segs(r, "m1")[0] == (0, 0, "subscribe")
    assert segs(r, "b")[0] == (1, 1, "subscribe")
    assert held_months(r, "b").isdisjoint(held_months(r, "m1"))


def test_zero_price_bundle_is_excluded_and_noted():
    recs = [rec("b", 99, 0.0, 30, is_bundle=True, includes=["m1"], needs_price=True), rec("m1", 10, 8, 30)]
    r = build_rotation(plan_of(recs), TODAY)
    assert segs(r, "b") == []
    assert segs(r, "m1")[0] == (0, 0, "subscribe")
    assert "Set a price for B to plan it" in r.notes


def test_member_of_a_deleted_bundle_is_ignored_with_a_note():
    recs = [rec("b", 90, 20, 30, is_bundle=True, includes=["ghost", "m1"]), rec("m1", 10, 8, 30)]
    r = build_rotation(plan_of(recs), TODAY)
    assert "B lists an unknown service ghost; ignored" in r.notes
    assert segs(r, "b")[0] == (0, 0, "subscribe")
    assert held_months(r, "b").isdisjoint(held_months(r, "m1"))


def test_long_tail_is_capped_at_three_months_then_paused():
    r = build_rotation(plan_of([rec("a", hours=200)], pace=1.0), TODAY)
    assert segs(r, "a") == [(0, 2, "subscribe"), (3, 3, "cancel"), (4, 5, "pause")]
    assert r.services[0].segments[0].label == "Subscribe (3 mo)"


def test_picks_follow_top_shows_order():
    tops = [{"id": i, "title": f"T{i}"} for i in (5, 2, 9, 7)]
    scores = {5: 10, 2: 90, 7: 50}
    r = build_rotation(plan_of([rec("a", top_shows=tops, top_show_scores=scores)]), TODAY)
    assert [(p.show_id, p.title, p.interest_score) for p in r.picks["a"]] == [
        (5, "T5", 10),
        (2, "T2", 90),
        (9, "T9", None),
    ]


def test_notes_and_unassigned_count():
    r = build_rotation(plan_of([rec("a")], unassigned=3), TODAY)
    assert any(n.startswith("Pace: 1 h/day") for n in r.notes)
    assert "3 titles have no service assigned and are not planned." in r.notes
    assert r.unassigned_titles == 3
