"""Plannable hours: the part of a backlog the household is realistically going to watch (pure)."""

from typing import Iterable

from penguwatch.models import HouseholdTag, OptimizerRecommendation, Priority, ServiceConfig, Show, ShowStatus

# Share of a title's remaining hours that counts as plannable, by status.
STATUS_WEIGHT = {"watching": 1.0, "plantowatch": 0.5, "hold": 0.25}
# A high-priority or "both want it" title counts in full whatever its status.
PRIORITY_OR_BOTH_WEIGHT = 1.0

_EXCLUDED = (ShowStatus.COMPLETED, ShowStatus.DROPPED)


def plannable_hours(show: Show) -> float:
    """Remaining hours of one show times its weight (completed/dropped contribute 0)."""
    if show.status in _EXCLUDED:
        return 0.0
    hours = show.episodes_left * show.runtime_minutes / 60.0
    if show.priority == Priority.HIGH or show.household_tag == HouseholdTag.BOTH:
        return hours * PRIORITY_OR_BOTH_WEIGHT
    return hours * STATUS_WEIGHT.get(show.status.value, 0.0)


def total_plannable_hours(shows: Iterable[Show]) -> float:
    return sum(plannable_hours(s) for s in shows)


def unassigned_titles(shows: Iterable[Show]) -> int:
    """Non-completed, non-dropped shows that are not assigned to any service."""
    return sum(1 for s in shows if s.status not in _EXCLUDED and not s.service_id)


def apply_price_flags(rec: OptimizerRecommendation, svc: ServiceConfig, shows: Iterable[Show]) -> None:
    """Sets plannable hours and the free / needs-price flags; a free or unpriced service is never a cancel candidate."""
    rec.plannable_hours = round(total_plannable_hours(shows), 1)
    rec.is_free = not svc.monthly_cost and not svc.is_bundle
    rec.needs_price = svc.is_bundle and not svc.monthly_cost
    if not (rec.is_free or rec.needs_price):
        return
    if rec.verdict in ("unused", "finish_before_renewal", "long_tail"):
        rec.verdict = None
        rec.verdict_text = None
    if rec.action == "cancel_recommended":
        rec.action = "keep_active"
        rec.recommendation_reason = "Free service: no cost to keep." if rec.is_free else "Set a price for this bundle."


def tag_interest(rec: OptimizerRecommendation, scores: dict) -> None:
    """Records the likely-interest score of each top show (kept apart: top_shows entries have a pinned shape)."""
    rec.top_show_scores = {t["id"]: scores[t["id"]] for t in rec.top_shows if t["id"] in scores}
