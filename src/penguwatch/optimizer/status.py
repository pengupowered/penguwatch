"""One status per service: what the household should do about it, from billing period and use evidence (pure)."""

from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from penguwatch.models import OptimizerRecommendation as Rec, ServiceConfig, Show, ShowStatus

MAX_WATCHING_TITLES = 3
PAID_THROUGH_DAYS = 30  # a yearly service renewing within this many days needs a decision
DECISION_HORIZON_DAYS = 45

Result = Tuple[str, str, float, Optional[str]]  # status, text, savable monthly cost, decide_by


def group_ids(svc: ServiceConfig) -> List[str]:
    """The service itself plus, for a bundle, its members."""
    return [svc.id] + (list(svc.includes) if svc.is_bundle else [])


def watching_titles(shows: Iterable[Show], ids: Iterable[str]) -> List[str]:
    """Titles on the Watching list with aired episodes left, assigned to one of `ids` (at most three)."""
    wanted = set(ids)
    out: List[str] = []
    for show in shows:
        if show.service_id in wanted and show.status == ShowStatus.WATCHING and show.episodes_left > 0:
            out.append(show.title)
            if len(out) == MAX_WATCHING_TITLES:
                break
    return out


def _watching_text(titles: List[str], window: int) -> str:
    name = titles[0] if len(titles) == 1 else ", ".join(titles)
    verb = "is" if len(titles) == 1 else "are"
    return (
        f"{name} {verb} in your Watching list but no episodes are marked watched in {window} days: "
        f"mark episodes as you watch so use is visible"
    )


def _used(rec: Rec, titles: List[str], unused_hours: float, window: int) -> Optional[Result]:
    """in_use / watching when there is viewing evidence, else None."""
    if rec.hours_watched_window >= unused_hours:
        return "in_use", f"{rec.hours_watched_window:g} h watched in the last {window} days", 0.0, None
    if titles:
        return "watching", _watching_text(titles, window), 0.0, None
    return None


def _yearly(rec: Rec, titles: List[str], unused_hours: float, window: int) -> Result:
    if rec.next_renewal is None or rec.renewal_in_days is None:
        return "check", "Add the renewal date so cancel advice can be timed", 0.0, None
    renews = date.fromisoformat(rec.next_renewal)
    if rec.renewal_in_days > PAID_THROUGH_DAYS:
        decide_by = (renews - timedelta(days=PAID_THROUGH_DAYS)).isoformat()
        return "paid_through", f"Paid through {rec.next_renewal}; nothing to cancel until then", 0.0, decide_by
    used = _used(rec, titles, unused_hours, window)
    if used:
        return used
    text = f"Renews {rec.next_renewal}; no recent viewing: decide before then"
    return "decide_soon", text, rec.monthly_cost, rec.next_renewal


def _monthly(rec: Rec, titles: List[str], titles_assigned: bool, unused_hours: float, window: int) -> Result:
    used = _used(rec, titles, unused_hours, window)
    if used:
        return used
    if not titles_assigned:
        return "check", "No titles are assigned to it yet, so use can't be seen: assign titles in Tidy", 0.0, None
    if rec.verdict == "unused":
        text = (
            f"No viewing in {window} days and nothing in your Watching list: "
            f"cancelling saves ${rec.monthly_cost:.2f}/mo"
        )
        return "consider_cancel", text, rec.monthly_cost, None
    return "in_use", rec.verdict_text or "In use", 0.0, None


def classify(
    rec: Rec, svc: ServiceConfig, titles: List[str], titles_assigned: bool, unused_hours: float, window: int
) -> Result:
    """Status for one recommendation; first matching rule wins."""
    if rec.covered_by:
        return "covered", rec.verdict_text or "Included in a bundle", 0.0, None
    if not rec.is_active:
        return "inactive", "", 0.0, None
    if rec.is_free:
        note = (svc.notes or "").strip()
        return "included", f"Included, no extra cost: {note}" if note else "Included, no extra cost", 0.0, None
    if svc.billing_period == "yearly":
        return _yearly(rec, titles, unused_hours, window)
    return _monthly(rec, titles, titles_assigned, unused_hours, window)


def next_decision(recs: Iterable[Rec], today: date) -> Optional[Dict[str, Any]]:
    """The nearest date at which a paid service needs a decision, within the horizon."""
    best: Optional[Tuple[date, Dict[str, Any]]] = None
    for rec in recs:
        if rec.decide_by:
            when, kind = date.fromisoformat(rec.decide_by), "decide"
        elif rec.status == "consider_cancel" and rec.next_renewal:
            when, kind = date.fromisoformat(rec.next_renewal), "renewal"
        else:
            continue
        days = (when - today).days
        if days > DECISION_HORIZON_DAYS:
            continue
        item = {"service_id": rec.service_id, "service_name": rec.service_name, "date": when.isoformat(),
                "days": max(0, days), "kind": kind}
        if best is None or when < best[0]:
            best = (when, item)
    return best[1] if best else None


def apply_status(
    recs: List[Rec], services: List[ServiceConfig], shows: List[Show], today: date, window: int, unused_hours: float
) -> Tuple[float, Optional[Dict[str, Any]]]:
    """Sets status fields on every recommendation; returns (savable monthly cost, next decision)."""
    by_id = {s.id: s for s in services}
    assigned = {s.service_id for s in shows if s.service_id}
    total = 0.0
    for rec in recs:
        svc = by_id.get(rec.service_id)
        if svc is None:
            continue
        ids = group_ids(svc)
        titles = watching_titles(shows, ids)
        status, text, savable, decide_by = classify(
            rec, svc, titles, any(i in assigned for i in ids), unused_hours, window
        )
        rec.billing_period = svc.billing_period
        rec.status, rec.status_text, rec.decide_by = status, text, decide_by
        rec.watching_titles = titles
        rec.savable_monthly_cost = round(savable, 2)
        total += savable
    return round(total, 2), next_decision(recs, today)
