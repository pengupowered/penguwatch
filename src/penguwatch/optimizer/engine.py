"""Subscription Rotation & Binge Planner Optimization Engine."""

import logging
import math
from datetime import date
from typing import List, Dict, Any, Optional, Set
from penguwatch import clock
from penguwatch.interest import build_profile, score
from penguwatch.optimizer import bundles
from penguwatch.optimizer.renewal import next_renewal  # noqa: F401 (re-exported)
from penguwatch.optimizer.status import apply_status
from penguwatch.optimizer.plannable import PRIORITY_OR_BOTH_WEIGHT, STATUS_WEIGHT, plannable_hours  # noqa: F401 (re-exported)
from penguwatch.optimizer.plannable import apply_price_flags, tag_interest, total_plannable_hours, unassigned_titles
from penguwatch.models import (
    BundleSuggestion,
    Show,
    ServiceConfig,
    OptimizerRecommendation,
    OptimizerPlan,
    ShowStatus,
    Priority,
    HouseholdTag,
)
from penguwatch.config import OptimizerConfig
from penguwatch.db.repository import DatabaseRepository

logger = logging.getLogger("penguwatch.optimizer")

VALUE_WINDOW_DAYS = 90
# Below this many hours in the window a service counts as "not used" (one episode of a 24-min anime)
UNUSED_HOURS = 0.5
# Backlog hours count towards a service's binge score only up to this many hours (~2 months at the
# household's pace). Uncapped, one 900 h long-running series outweighs a dozen ready-to-binge shows.
BACKLOG_CAP_HOURS = 60.0


class SubscriptionOptimizer:
    def __init__(self, repository: DatabaseRepository, config: OptimizerConfig, value_window_days: int = VALUE_WINDOW_DAYS):
        self.repository = repository
        self.config = config
        self.value_window_days = value_window_days

    async def calculate_plan(self) -> OptimizerPlan:
        """Calculates optimal subscription rotation, binge readiness scores, and savings."""
        services = await self.repository.get_services()
        all_shows = await self.repository.get_shows()
        viewing = await self.repository.get_service_value(self.value_window_days)
        cov = bundles.coverage(services)
        recommendations, active_services = self._build_recommendations(services, all_shows, viewing, cov, clock.today())
        all_spend = sum(s.monthly_cost for s in services)
        current_spend = bundles.monthly_spend(services)

        # Sort recommendations:
        # First active services that should be kept, then inactive services sorted by binge score descending
        def sort_key(r: OptimizerRecommendation):
            # Active services come first
            active_prio = 0 if r.is_active else 1
            # Higher binge score is better
            return (active_prio, -r.binge_score)

        recommendations.sort(key=sort_key)

        redundant = bundles.redundant_bundle_ids(services, cov)
        self._mark_activate_now(recommendations, redundant)

        monthly_savings = max(0.0, all_spend - current_spend)
        annual_savings = monthly_savings * 12.0

        # Generate rotation advice narrative
        rotation_advice = self._generate_rotation_advice(
            active_services, recommendations, self.config.max_active_services, redundant
        )
        suggestions = self._bundle_suggestions(services, cov, viewing)
        rotation_advice.extend(f"Bundle: {s.text}" for s in suggestions)

        unused_cost = sum(r.monthly_cost for r in recommendations if r.verdict == "unused")
        household_hours = sum(v.get("hours", 0.0) for v in viewing.values())
        savable, next_decision = apply_status(
            recommendations, services, all_shows, clock.today(), self.value_window_days, UNUSED_HOURS
        )

        return OptimizerPlan(
            value_window_days=self.value_window_days,
            unused_monthly_cost=round(unused_cost, 2),
            household_hours_per_day=round(household_hours / max(1, self.value_window_days), 2),
            current_monthly_spend=round(current_spend, 2),
            all_services_spend=round(all_spend, 2),
            monthly_savings=round(monthly_savings, 2),
            annual_savings=round(annual_savings, 2),
            max_active_services=self.config.max_active_services,
            active_services=active_services,
            service_rankings=recommendations,
            rotation_advice=rotation_advice,
            bundle_suggestions=suggestions,
            unassigned_titles=unassigned_titles(all_shows),
            savable_monthly_cost=savable,
            next_decision=next_decision,
        )

    @staticmethod
    def _mark_activate_now(recommendations: List[OptimizerRecommendation], redundant: Set[str]) -> None:
        """Mark the top inactive service as "activate_now" (unless it waits for a finale)."""
        inactive_recs = [
            r for r in recommendations
            if not r.is_active and r.binge_score > 0 and r.service_id not in redundant and not r.needs_price
        ]
        if inactive_recs:
            top_inactive = inactive_recs[0]
            if top_inactive.action != "wait_for_finale":
                top_inactive.action = "activate_now"
                top_inactive.recommendation_reason = (
                    f"Top recommended service to activate next! {top_inactive.ready_to_binge_count} "
                    f"shows ready to binge ({round(top_inactive.hours_backlog, 1)} hrs backlog)."
                )

    def _build_recommendations(
        self,
        services: List[ServiceConfig],
        all_shows: List[Show],
        viewing: Dict[str, Dict[str, float]],
        cov: Dict[str, str],
        today: date,
    ):
        """One recommendation per service (bundle- and coverage-aware) plus the effectively active services."""
        service_shows: Dict[str, List[Show]] = {s.id: [] for s in services}
        for show in all_shows:
            # Only consider shows that are not completed or dropped
            if show.status not in (ShowStatus.COMPLETED, ShowStatus.DROPPED):
                if show.service_id and show.service_id in service_shows:
                    service_shows[show.service_id].append(show)

        profile = build_profile(all_shows)
        scores = {s.id: score(s, profile)[0] for group in service_shows.values() for s in group}
        by_id = {s.id: s for s in services}
        recommendations: List[OptimizerRecommendation] = []
        active_services: List[ServiceConfig] = []
        for svc in services:
            if bundles.is_effectively_active(svc, cov):
                active_services.append(svc)
            members = [by_id[m] for m in svc.includes if m in by_id] if svc.is_bundle else []
            shows = self._union_shows(service_shows, [svc] + members)
            seen = self._union_viewing(viewing, [svc] + members)
            rec = self._evaluate_service(svc, shows, scores)
            self._apply_value(rec, svc, seen, today)
            apply_price_flags(rec, svc, shows)
            tag_interest(rec, scores)
            self._apply_bundles(rec, svc, by_id, cov)
            recommendations.append(rec)
        return recommendations, active_services

    @staticmethod
    def _union_shows(service_shows: Dict[str, List[Show]], group: List[ServiceConfig]) -> List[Show]:
        """Shows of every service in the group, deduplicated by show id (a plain service: its own list)."""
        if len(group) == 1:
            return service_shows.get(group[0].id, [])
        out: List[Show] = []
        seen = set()
        for svc in group:
            for show in service_shows.get(svc.id, []):
                if show.id not in seen:
                    seen.add(show.id)
                    out.append(show)
        return out

    @staticmethod
    def _union_viewing(viewing: Dict[str, Dict[str, float]], group: List[ServiceConfig]) -> Dict[str, float]:
        """Hours and episodes watched, summed over the group (a plain service: its own entry)."""
        if len(group) == 1:
            return viewing.get(group[0].id, {})
        entries = [viewing.get(s.id, {}) for s in group]
        return {
            "hours": sum(e.get("hours", 0.0) for e in entries),
            "episodes": sum(e.get("episodes", 0) for e in entries),
        }

    @staticmethod
    def _apply_bundles(
        rec: OptimizerRecommendation, svc: ServiceConfig, by_id: Dict[str, ServiceConfig], cov: Dict[str, str]
    ) -> None:
        """Flags a bundle and its members; a covered member is held, free and billed through the bundle."""
        if svc.is_bundle:
            rec.is_bundle = True
            rec.includes = list(svc.includes)
        bundle = by_id.get(cov.get(svc.id, ""))
        if bundle is None:
            return
        rec.covered_by = bundle.id
        rec.is_active = True
        rec.monthly_cost = 0.0
        rec.cost_per_hour = None
        rec.next_renewal = None
        rec.renewal_in_days = None
        rec.verdict = "covered"
        rec.verdict_text = f"Included in {bundle.name}"
        rec.action = "keep_active"
        rec.recommendation_reason = f"Included in {bundle.name}."

    def _bundle_suggestions(
        self, services: List[ServiceConfig], cov: Dict[str, str], viewing: Dict[str, Dict[str, float]]
    ) -> List[BundleSuggestion]:
        hours = {sid: v.get("hours", 0.0) for sid, v in viewing.items()}
        return bundles.switch_suggestions(services, cov) + bundles.partial_use_suggestions(
            services, hours, UNUSED_HOURS, self.value_window_days
        )

    def _apply_value(self, rec: OptimizerRecommendation, svc: ServiceConfig, seen: Dict[str, float], today: date) -> None:
        """Adds viewing value, pace and a cancel/keep verdict from what was actually watched."""
        window = self.value_window_days
        hours = round(seen.get("hours", 0.0), 1)
        rec.episodes_watched_window = int(seen.get("episodes", 0))
        rec.hours_watched_window = hours
        pace = hours / window if window else 0.0
        rec.pace_hours_per_day = round(pace, 3)
        if svc.is_active and svc.monthly_cost and hours > 0:
            rec.cost_per_hour = round(svc.monthly_cost * (window / 30.44) / hours, 2)
        if pace > 0 and rec.hours_backlog > 0:
            rec.days_to_clear = math.ceil(rec.hours_backlog / pace)
        renewal = next_renewal(svc, today)
        if renewal:
            rec.next_renewal = renewal.isoformat()
            rec.renewal_in_days = (renewal - today).days

        if not svc.is_active:
            return
        if hours < UNUSED_HOURS:
            rec.verdict = "unused"
            rec.verdict_text = (
                f"No viewing on {svc.name} in the last {window} days. Cancelling saves "
                f"${svc.monthly_cost:.2f}/mo (${svc.monthly_cost * 12:.0f}/yr)."
            )
        elif rec.days_to_clear is not None and rec.renewal_in_days is not None and rec.days_to_clear <= rec.renewal_in_days:
            rec.verdict = "finish_before_renewal"
            rec.verdict_text = (
                f"At your pace the {rec.hours_backlog:.0f} h backlog is done in about {rec.days_to_clear} days, "
                f"before the renewal in {rec.renewal_in_days} days. Cancel before {rec.next_renewal}."
            )
        elif rec.days_to_clear is not None and rec.days_to_clear > 180:
            rec.verdict = "long_tail"
            rec.verdict_text = (
                f"{rec.hours_backlog:.0f} h of backlog is about {rec.days_to_clear} days at your pace here. "
                f"Pause between binges instead of paying monthly."
            )
        else:
            rec.verdict = "in_use"
            rec.verdict_text = f"In use: {hours:.0f} h watched in {window} days."

    @staticmethod
    def _order_shows(shows: List[Show], scores: Optional[Dict[int, int]]) -> List[Show]:
        """Likely-interest score (then IMDb, title) when scores are given, else priority then IMDb."""
        if scores is not None:
            return sorted(shows, key=lambda s: (-scores.get(s.id, 0), -(s.imdb_rating or 0.0), s.title))
        return sorted(
            shows,
            key=lambda s: (
                0 if s.priority == Priority.HIGH else (1 if s.priority == Priority.MEDIUM else 2),
                -(s.imdb_rating or 0.0),
            ),
        )

    def _evaluate_service(
        self, svc: ServiceConfig, shows: List[Show], scores: Optional[Dict[int, int]] = None
    ) -> OptimizerRecommendation:
        total_episodes_backlog = 0
        total_hours_backlog = 0.0
        ready_to_binge_count = 0
        both_want_count = 0
        high_priority_count = 0
        imdb_ratings = []
        top_shows = []

        sorted_shows = self._order_shows(shows, scores)

        for show in sorted_shows:
            episodes_left = show.episodes_left
            if episodes_left > 0:
                total_episodes_backlog += episodes_left
                total_hours_backlog += episodes_left * (show.runtime_minutes / 60.0)

            if show.is_ready_to_binge:
                ready_to_binge_count += 1
            if show.household_tag == HouseholdTag.BOTH:
                both_want_count += 1
            if show.priority == Priority.HIGH:
                high_priority_count += 1
            if show.imdb_rating:
                imdb_ratings.append(show.imdb_rating)

            if len(top_shows) < 3:
                top_shows.append(
                    {
                        "id": show.id,
                        "title": show.title,
                        "year": show.year,
                        "poster": show.poster,
                        "imdb_rating": show.imdb_rating,
                        "episodes_left": episodes_left,
                        "is_ready_to_binge": show.is_ready_to_binge,
                        "status": show.status.value,
                        "priority": show.priority.value,
                        "household_tag": show.household_tag.value,
                    }
                )

        avg_imdb = (sum(imdb_ratings) / len(imdb_ratings)) if imdb_ratings else None

        # Binge Score calculation
        # Backlog hours + boosts for ready-to-binge + both want + quality
        score = min(total_plannable_hours(shows), BACKLOG_CAP_HOURS) * 1.0
        score += ready_to_binge_count * (self.config.ready_to_binge_boost * 10.0)
        score += both_want_count * (self.config.household_priority_boost * 10.0)
        score += high_priority_count * 15.0
        if avg_imdb:
            score += max(0.0, (avg_imdb - 6.0) * 8.0)

        # Decide Action & Reason
        action = "standby"
        reason = f"Backlog: {len(shows)} shows ({round(total_hours_backlog, 1)} hrs)."

        if svc.is_active:
            if total_episodes_backlog == 0 and len(shows) > 0:
                action = "cancel_recommended"
                reason = "All queued shows completed! Cancel subscription to stop recurring billing and rotate."
            elif total_episodes_backlog == 0 and len(shows) == 0:
                action = "cancel_recommended"
                reason = "No shows tracked on this service. Safe to cancel to save money."
            else:
                action = "keep_active"
                reason = f"Active subscription: {total_episodes_backlog} unwatched episodes remaining."
        else:
            # Check if any show has an upcoming air date / season finale
            has_airing_soon = any(
                s.next_episode_air_date is not None and not s.is_ready_to_binge for s in shows
            )
            if has_airing_soon and ready_to_binge_count == 0 and total_hours_backlog > 0:
                action = "wait_for_finale"
                reason = "Shows currently airing weekly episodes. Wait for season finale before subscribing to binge in 1 month!"
            elif len(shows) > 0:
                action = "standby"
                reason = f"{ready_to_binge_count} shows ready to binge ({round(total_hours_backlog, 1)} hrs total)."
            else:
                action = "standby"
                reason = "No shows currently queued on this service."

        return OptimizerRecommendation(
            service_id=svc.id,
            service_name=svc.name,
            is_active=svc.is_active,
            monthly_cost=svc.monthly_cost,
            binge_score=round(score, 1),
            hours_backlog=round(total_hours_backlog, 1),
            episodes_backlog=total_episodes_backlog,
            shows_count=len(shows),
            ready_to_binge_count=ready_to_binge_count,
            both_want_count=both_want_count,
            avg_imdb_rating=round(avg_imdb, 1) if avg_imdb else None,
            top_shows=top_shows,
            recommendation_reason=reason,
            action=action,
        )

    def _generate_rotation_advice(
        self,
        active_services: List[ServiceConfig],
        recommendations: List[OptimizerRecommendation],
        max_active: int,
        skip_ids: Optional[Set[str]] = None,
    ) -> List[str]:
        advice = []

        for r in recommendations:
            if r.verdict == "unused":
                advice.append(f"Unused: {r.verdict_text}")
            elif r.needs_price:
                advice.append(f"Set a price for {r.service_name}")
        advice.append(self._focus_advice(active_services, recommendations, max_active))

        # Check for cancel recommendations
        cancels = [r for r in recommendations if r.action == "cancel_recommended"]
        for c in cancels:
            advice.append(
                f"Ready to Rotate: You've completed your backlog on {c.service_name}! "
                f"Cancel now to save ${c.monthly_cost:.2f}/mo."
            )

        next_line = self._next_up_advice(recommendations, skip_ids or set())
        if next_line:
            advice.append(next_line)
        return advice

    @staticmethod
    def _focus_advice(
        active_services: List[ServiceConfig], recommendations: List[OptimizerRecommendation], max_active: int
    ) -> str:
        if not active_services and recommendations:
            return (
                f"You currently have 0 active subscriptions. We recommend activating "
                f"'{recommendations[0].service_name}' first based on your household queue."
            )
        paid = [r for r in recommendations if r.is_active and r.monthly_cost > 0]  # free and bundle-covered use no slot
        if len(paid) > max_active:
            return (
                f"You currently have {len(paid)} paid active services, exceeding your target limit of {max_active}. "
                f"Consider pausing services with smaller backlogs to maximize monthly savings."
            )
        active_names = ", ".join(s.name for s in active_services)
        return (
            f"Current focus: You are subscribed to {active_names}. "
            f"Focus on binging high-priority shared shows on these platforms."
        )

    @staticmethod
    def _next_up_advice(recommendations: List[OptimizerRecommendation], skip: Set[str]) -> Optional[str]:
        next_up = [
            r for r in recommendations
            if not r.is_active and r.binge_score > 0 and r.service_id not in skip and not r.needs_price
        ]
        if not next_up:
            return None
        top_next = next_up[0]
        top_show_titles = [s["title"] for s in top_next.top_shows[:2]]
        shows_str = f" ({', '.join(top_show_titles)})" if top_show_titles else ""
        return (
            f"Next in Rotation: Rotate to {top_next.service_name}{shows_str} "
            f"which has {top_next.ready_to_binge_count} shows ready to binge."
        )
