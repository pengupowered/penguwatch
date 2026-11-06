"""Next billing date of a service (pure)."""

import calendar
from datetime import date
from typing import Optional

from penguwatch.models import ServiceConfig


def _clip(y: int, m: int, d: int) -> date:
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def _add_month(d: date, anchor_day: int) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return _clip(y, m, anchor_day)


def _anchor(svc: ServiceConfig) -> Optional[date]:
    if not svc.renewal_date:
        return None
    try:
        return date.fromisoformat(svc.renewal_date[:10])
    except ValueError:
        return None


def _roll_yearly(anchor: date, today: date) -> Optional[date]:
    """First anniversary of `anchor` that is today or later (Feb 29 becomes Feb 28 in common years)."""
    cur = anchor
    for _ in range(100):
        if cur >= today:
            return cur
        cur = _clip(cur.year + 1, anchor.month, anchor.day)
    return None


def next_renewal(svc: ServiceConfig, today: date) -> Optional[date]:
    """Next billing date: renewal_date rolled forward by a month (or a year for a yearly service).

    A monthly service falls back to renewal_day; a yearly one has no day-of-month and returns None
    without a renewal_date.
    """
    anchor = _anchor(svc)
    if svc.billing_period == "yearly":
        return _roll_yearly(anchor, today) if anchor else None
    if anchor:
        cur = anchor
        for _ in range(600):  # 50 years of months is plenty
            if cur >= today:
                return cur
            cur = _add_month(cur, anchor.day)
    if svc.renewal_day:
        this_month = _clip(today.year, today.month, svc.renewal_day)
        return this_month if this_month >= today else _add_month(this_month, svc.renewal_day)
    return None
