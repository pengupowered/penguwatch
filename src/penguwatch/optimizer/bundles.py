"""Bundle coverage and bundle-aware spend (pure: no I/O, no DB)."""

from typing import Dict, List, Mapping, Optional, Sequence, Set

from penguwatch.models import BundleSuggestion, ServiceConfig


def coverage(services: Sequence[ServiceConfig]) -> Dict[str, str]:
    """member service id -> id of an ACTIVE bundle that includes it.

    If several active bundles include the same member the cheapest bundle wins
    (ties broken by bundle id). Bundles never cover other bundles.
    """
    bundle_ids = {s.id for s in services if s.is_bundle}
    active = sorted((s for s in services if s.is_bundle and s.is_active), key=lambda b: (b.monthly_cost, b.id))
    cov: Dict[str, str] = {}
    for bundle in active:
        for member in bundle.includes:
            if member == bundle.id or member in bundle_ids or member in cov:
                continue
            cov[member] = bundle.id
    return cov


def is_effectively_active(svc: ServiceConfig, cov: Mapping[str, str]) -> bool:
    return svc.is_active or svc.id in cov


def monthly_spend(services: Sequence[ServiceConfig]) -> float:
    """Sum of monthly_cost over active services that are NOT covered by another active bundle."""
    cov = coverage(services)
    total = sum((s.monthly_cost or 0.0) for s in services if s.is_active and s.id not in cov)
    return round(total, 2)


def paying_count(services: Sequence[ServiceConfig]) -> int:
    """Number of active services that are not covered by a bundle (a bundle counts as one)."""
    cov = coverage(services)
    return sum(1 for s in services if s.is_active and s.id not in cov)


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def redundant_bundle_ids(services: Sequence[ServiceConfig], cov: Mapping[str, str]) -> Set[str]:
    """Inactive bundles whose members are all effectively active already (nothing left to activate)."""
    by_id = {s.id: s for s in services}
    return {
        s.id
        for s in services
        if s.is_bundle and not s.is_active and s.includes
        and all(m in by_id and is_effectively_active(by_id[m], cov) for m in s.includes)
    }


def _switch_members(
    bundle: ServiceConfig, by_id: Mapping[str, ServiceConfig], cov: Mapping[str, str]
) -> Optional[List[ServiceConfig]]:
    """Members of an inactive bundle that are all paid for separately now, else None."""
    if not bundle.is_bundle or bundle.is_active or not bundle.includes or not bundle.monthly_cost:
        return None
    members = [by_id.get(m) for m in bundle.includes]
    if any(m is None or not m.is_active or m.id in cov for m in members):
        return None
    return members  # type: ignore[return-value]


def switch_suggestions(services: Sequence[ServiceConfig], cov: Mapping[str, str]) -> List[BundleSuggestion]:
    """Inactive bundles that cost strictly less than the members now paid for separately."""
    by_id = {s.id: s for s in services}
    out: List[BundleSuggestion] = []
    for bundle in services:
        members = _switch_members(bundle, by_id, cov)
        if members is None:
            continue
        current = round(sum((m.monthly_cost or 0.0) for m in members), 2)
        if current <= (bundle.monthly_cost or 0.0):
            continue
        cost = bundle.monthly_cost or 0.0
        saving = round(current - cost, 2)
        names = _join([m.name for m in members])
        out.append(BundleSuggestion(
            kind="switch_to_bundle", bundle_id=bundle.id, bundle_name=bundle.name,
            member_ids=list(bundle.includes), current_cost=current, bundle_cost=cost, monthly_saving=saving,
            text=(f"{names} are active separately (${current:.2f}/mo). {bundle.name} costs "
                  f"${cost:.2f}/mo: switching saves ${saving:.2f}/mo."),
        ))
    return out


def partial_use_suggestions(
    services: Sequence[ServiceConfig], hours: Mapping[str, float], min_hours: float, window_days: int
) -> List[BundleSuggestion]:
    """Active bundles where only some members were watched and those alone cost strictly less."""
    by_id = {s.id: s for s in services}
    out: List[BundleSuggestion] = []
    for bundle in services:
        if not bundle.is_bundle or not bundle.is_active:
            continue
        members = [by_id[m] for m in bundle.includes if m in by_id]
        watched = [m for m in members if round(hours.get(m.id, 0.0), 1) >= min_hours]
        if not 0 < len(watched) < len(members):
            continue
        alone = round(sum((m.monthly_cost or 0.0) for m in watched), 2)
        cost = bundle.monthly_cost or 0.0
        if alone >= cost:
            continue
        saving = round(cost - alone, 2)
        names = _join([m.name for m in watched])
        verb = "was" if len(watched) == 1 else "were"
        out.append(BundleSuggestion(
            kind="bundle_not_worth_it", bundle_id=bundle.id, bundle_name=bundle.name,
            member_ids=[m.id for m in watched], current_cost=cost, bundle_cost=cost, monthly_saving=saving,
            text=(f"Only {names} {verb} watched in the last {window_days} days. Dropping {bundle.name} "
                  f"(${cost:.2f}/mo) for {names} alone (${alone:.2f}/mo) saves ${saving:.2f}/mo."),
        ))
    return out
