"""Bundle-aware rotation planner: which service to hold in which month (pure, no I/O)."""

import math
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from penguwatch.models import (
    OptimizerPlan,
    OptimizerRecommendation as Rec,
    RotationPick,
    RotationPlan,
    RotationSegment,
    RotationService,
    RotationSummary,
)

PACE_FLOOR = 0.25  # h/day
MAX_MONTHS_PER_SERVICE = 3
DAYS_PER_MONTH = 30
MAX_PICKS = 3
WAIT_LABEL = "Weekly airing: subscribe once a full season is ready"

Cell = Tuple[str, str]  # (kind, label)


class _Grid:
    """Per-service month cells plus the months in which a paid, non-covered service is held."""

    def __init__(self, months: int) -> None:
        self.months = months
        self.cells: Dict[str, Dict[int, Cell]] = {}
        self.held: Dict[str, Set[int]] = {}

    def put(self, sid: str, month: int, kind: str, label: str, paid: bool = True) -> None:
        if not 0 <= month < self.months:
            return
        self.cells.setdefault(sid, {})[month] = (kind, label)
        if paid and kind in ("keep", "subscribe"):
            self.held.setdefault(sid, set()).add(month)

    def stint(self, sid: str, start: int, length: int, kind: str, label: str, cancel_label: str) -> None:
        """Held for `length` months from `start`, a cancel marker right after, then paused."""
        for m in range(start, start + length):
            self.put(sid, m, kind, label)
        end = start + length
        self.put(sid, end, "cancel", cancel_label)
        for m in range(end + 1, self.months):
            self.put(sid, m, "pause", "Paused")

    def used(self, month: int) -> int:
        return sum(1 for months in self.held.values() if month in months)

    def is_held(self, sid: str, month: int) -> bool:
        return month in self.held.get(sid, ())


def _needed_months(rec: Rec, pace: float) -> int:
    """Months of viewing the plannable backlog needs at this pace, clamped to 1..MAX_MONTHS_PER_SERVICE."""
    needed = math.ceil(rec.plannable_hours / (pace * DAYS_PER_MONTH))
    return max(1, min(MAX_MONTHS_PER_SERVICE, needed))


def _cancel_month(rec: Rec) -> int:
    """Month in which the next renewal falls (0 when unknown or within a month)."""
    days = rec.renewal_in_days
    if days is None or days < DAYS_PER_MONTH:
        return 0
    return days // DAYS_PER_MONTH


def _cancel_label(rec: Rec, month: int) -> str:
    return "Cancel now" if rec.renewal_in_days is None and month == 0 else "Cancel before renewal"


def _rank_key(rec: Rec) -> Tuple[float, float, str]:
    """Highest score first; on a tie the cheaper, then the id."""
    return (-rec.binge_score, rec.monthly_cost, rec.service_id)


def _conflicts(recs: List[Rec]) -> Dict[str, Set[str]]:
    """Bundle/member overlap: each bundle and each known member exclude one another."""
    known = {r.service_id for r in recs}
    out: Dict[str, Set[str]] = {r.service_id: set() for r in recs}
    for rec in recs:
        if not rec.is_bundle:
            continue
        for member in rec.includes:
            if member in known and member != rec.service_id:
                out[rec.service_id].add(member)
                out[member].add(rec.service_id)
    return out


def _eligible(rec: Rec, scheduled: Set[str]) -> bool:
    """An inactive, unscheduled service with a ready backlog that is not weekly-airing."""
    return (
        not rec.is_active
        and not rec.is_free
        and rec.covered_by is None
        and not rec.needs_price
        and rec.action != "wait_for_finale"
        and rec.plannable_hours > 0
        and rec.ready_to_binge_count > 0
        and rec.service_id not in scheduled
    )


def _pick_for_slot(
    recs: List[Rec], grid: _Grid, month: int, pace: float, conflicts: Dict[str, Set[str]], scheduled: Set[str]
) -> Optional[Rec]:
    """Highest-scored eligible candidate whose stint overlaps no conflicting held service."""
    for rec in sorted((r for r in recs if _eligible(r, scheduled)), key=_rank_key):
        span = range(month, min(grid.months, month + _needed_months(rec, pace)))
        if not any(grid.is_held(other, m) for other in conflicts[rec.service_id] for m in span):
            return rec
    return None


def _release_at_renewal(rec: Rec, grid: _Grid, notes: List[str]) -> None:
    """Held until its next renewal, cancelled there, paused afterwards."""
    month = _cancel_month(rec)
    if rec.renewal_in_days is None:
        notes.append(f"Check your renewal day for {rec.service_name}")
    grid.stint(rec.service_id, 0, month, "keep", "Keep", _cancel_label(rec, month))


# Statuses (optimizer/status.py) the over-slot rule never cancels. A service that is in use is never
# told to "cancel now": being over the slot target only stops new subscriptions being scheduled.
_HELD_STATUSES = ("included", "paid_through", "watching", "check", "in_use")
_CANCEL_STATUSES = ("consider_cancel", "decide_soon")


def _cancel_now(rec: Rec) -> bool:
    """Rule 1: no viewing evidence. Without a status (hand-built plans) the verdict decides, as before."""
    if rec.status is None:
        return rec.verdict == "unused"
    return rec.status in _CANCEL_STATUSES


def _hold_until_renewal(rec: Rec, grid: _Grid) -> None:
    """A yearly service paid through its renewal: held until that month, then judged like a monthly one."""
    month = _cancel_month(rec)
    if rec.verdict == "unused" and not rec.watching_titles:
        grid.stint(rec.service_id, 0, month, "keep", "Keep", _cancel_label(rec, month))
    else:
        for m in range(grid.months):
            grid.put(rec.service_id, m, "keep", "Keep")


def _keep_or_release(rec: Rec, grid: _Grid, pace: float, notes: List[str]) -> None:
    if rec.status == "check":
        # A data gap (no titles assigned, or a yearly plan with no date) is never grounds for a cancel marker.
        for m in range(grid.months):
            grid.put(rec.service_id, m, "keep", "Keep")
    elif rec.plannable_hours <= 0:
        _release_at_renewal(rec, grid, notes)
    else:
        grid.stint(rec.service_id, 0, _needed_months(rec, pace), "keep", "Keep", "Cancel before renewal")


def _plan_active(recs: List[Rec], grid: _Grid, slots: int, pace: float, notes: List[str]) -> None:
    """Rules 1 and 2: cancel unused services, keep the best `slots` of the rest, cancel the others."""
    paid = [r for r in recs if r.is_active and not r.is_free and r.covered_by is None]
    rest = []
    for rec in paid:
        if _cancel_now(rec):
            _release_at_renewal(rec, grid, notes)
        elif rec.status == "paid_through":
            _hold_until_renewal(rec, grid)
        else:
            rest.append(rec)
    rest.sort(key=_rank_key)
    for rec in rest[:slots]:
        _keep_or_release(rec, grid, pace, notes)
    overflow_in_use = [r for r in rest[slots:] if r.status == "in_use"]
    for rec in rest[slots:]:
        if rec.status in _HELD_STATUSES:
            _keep_or_release(rec, grid, pace, notes)
        else:
            _release_at_renewal(rec, grid, notes)
    if overflow_in_use:
        names = ", ".join(r.service_name for r in overflow_in_use)
        notes.append(
            f"{len(rest)} paid services are in use, above your target of {slots}: nothing new is scheduled "
            f"until one finishes (lowest use: {names})"
        )


def _plan_static(recs: List[Rec], grid: _Grid) -> None:
    """Free services are kept throughout; weekly-airing inactive services wait."""
    for rec in recs:
        if rec.is_free:
            for m in range(grid.months):
                grid.put(rec.service_id, m, "keep", "Free", paid=False)
        elif not rec.is_active and rec.covered_by is None and rec.action == "wait_for_finale":
            for m in range(grid.months):
                grid.put(rec.service_id, m, "wait", WAIT_LABEL, paid=False)


def _plan_covered(recs: List[Rec], grid: _Grid) -> None:
    """Members of an active bundle are kept for as long as the bundle is held."""
    names = {r.service_id: r.service_name for r in recs}
    for rec in recs:
        bundle = rec.covered_by
        if bundle is None or rec.is_free:
            continue
        label = f"Included in {names.get(bundle, bundle)}"
        for m in range(grid.months):
            kind = grid.cells.get(bundle, {}).get(m, ("keep", ""))[0]
            if kind in ("keep", "subscribe"):
                grid.put(rec.service_id, m, "keep", label, paid=False)
            else:
                grid.put(rec.service_id, m, "pause", "Paused", paid=False)


def _plan_free_slots(recs: List[Rec], grid: _Grid, slots: int, pace: float) -> None:
    """Rule 4: fill every free slot, month by month, with the best eligible candidate."""
    conflicts = _conflicts(recs)
    scheduled = set(grid.cells)
    for month in range(grid.months):
        while grid.used(month) < slots:
            rec = _pick_for_slot(recs, grid, month, pace, conflicts, scheduled)
            if rec is None:
                break
            length = _needed_months(rec, pace)
            grid.stint(rec.service_id, month, length, "subscribe", f"Subscribe ({length} mo)", "Cancel before renewal")
            scheduled.add(rec.service_id)


def _segments(cells: Dict[int, Cell]) -> List[RotationSegment]:
    """Contiguous months of the same kind and label merged into one segment."""
    out: List[RotationSegment] = []
    for month in sorted(cells):
        kind, label = cells[month]
        last = out[-1] if out else None
        if last and last.end_month == month - 1 and last.kind == kind and last.label == label:
            last.end_month = month
        else:
            out.append(RotationSegment(start_month=month, end_month=month, kind=kind, label=label))
    return out


def _picks(recs: List[Rec], grid: _Grid) -> Dict[str, List[RotationPick]]:
    out: Dict[str, List[RotationPick]] = {}
    for rec in recs:
        kinds = {kind for kind, _ in grid.cells.get(rec.service_id, {}).values()}
        if rec.is_free or not kinds & {"keep", "subscribe"}:
            continue
        out[rec.service_id] = [
            RotationPick(show_id=t["id"], title=t["title"], interest_score=rec.top_show_scores.get(t["id"]))
            for t in rec.top_shows[:MAX_PICKS]
        ]
    return out


def _summarise(recs: List[Rec], grid: _Grid, plan: OptimizerPlan) -> RotationSummary:
    cost = sum(r.monthly_cost * len(grid.held.get(r.service_id, ())) for r in recs if not r.is_free)
    planned = round(cost / grid.months, 2)
    cancel_now = [r.service_id for r in recs if grid.cells.get(r.service_id, {}).get(0, ("", ""))[0] == "cancel"]
    return RotationSummary(
        current_monthly_spend=plan.current_monthly_spend,
        planned_avg_monthly_spend=planned,
        monthly_difference=round(planned - plan.current_monthly_spend, 2),
        cancel_now=cancel_now,
    )


def _notes(plan: OptimizerPlan, pace: float, floored: bool, recs: List[Rec]) -> List[str]:
    notes = [f"Pace: {pace:g} h/day, the household's average over the last {plan.value_window_days} days."]
    if floored:
        notes.append(f"Pace below {PACE_FLOOR:g} h/day; planning with {PACE_FLOOR:g} h/day.")
    notes.append("No air dates are known; weekly-airing services are only marked wait.")
    notes.append("Prices as configured on the Services tab.")
    known = {r.service_id for r in recs}
    for rec in recs:
        if rec.needs_price:
            notes.append(f"Set a price for {rec.service_name} to plan it")
        notes.extend(
            f"{rec.service_name} lists an unknown service {m}; ignored"
            for m in (rec.includes if rec.is_bundle else [])
            if m not in known
        )
    return notes


def build_rotation(plan: OptimizerPlan, today: date, months: int = 6) -> RotationPlan:
    """Month-by-month rotation for the plan's services. `today` is informational: renewals come as days."""
    recs = list(plan.service_rankings)
    pace = max(plan.household_hours_per_day, PACE_FLOOR)
    floored = plan.household_hours_per_day < PACE_FLOOR
    slots = max(1, plan.max_active_services)
    grid = _Grid(months)
    notes = _notes(plan, pace, floored, recs)
    if recs:
        _plan_static(recs, grid)
        _plan_active(recs, grid, slots, pace, notes)
        _plan_covered(recs, grid)
        _plan_free_slots(recs, grid, slots, pace)
    else:
        notes = ["No services to plan yet."]
    if plan.unassigned_titles > 0:
        notes.append(f"{plan.unassigned_titles} titles have no service assigned and are not planned.")
    services = [
        RotationService(
            service_id=r.service_id,
            name=r.service_name,
            monthly_cost=r.monthly_cost,
            is_free=r.is_free,
            is_bundle=r.is_bundle,
            includes=list(r.includes),
            covered_by=r.covered_by,
            segments=_segments(grid.cells.get(r.service_id, {})),
        )
        for r in recs
    ]
    return RotationPlan(
        months=months,
        slots=slots,
        pace_hours_per_day=pace,
        pace_floor_applied=floored,
        unassigned_titles=plan.unassigned_titles,
        summary=_summarise(recs, grid, plan),
        services=services,
        picks=_picks(recs, grid),
        notes=notes,
    )
