"""iCal helpers: text escaping, line folding and the billing events (renewals, cancel-by reminders)."""

import re
from datetime import date, timedelta
from typing import Iterable, List

from penguwatch.models import OptimizerRecommendation as Rec

FOLD_OCTETS = 75  # RFC 5545: content lines are folded at 75 octets, continuation lines start with a space
CANCEL_STATUSES = ("consider_cancel", "decide_soon")  # the optimizer says to cancel (see optimizer/planner.py)


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # RFC 5545 TEXT: no control characters (a tab is allowed)


def escape(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")  # a bare CR must not break a content line
    text = _CONTROL.sub("", text)
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> List[str]:
    """One content line as physical lines of at most 75 octets, never splitting a UTF-8 character."""
    out: List[str] = []
    cur, size, limit = "", 0, FOLD_OCTETS
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, FOLD_OCTETS  # the leading space counts towards the next line's limit
        cur += ch
        size += n
    out.append(cur)
    return out


def _all_day(uid: str, stamp: str, day: date, summary: str, description: str) -> List[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}@penguwatch",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{escape(summary)}",
        f"DESCRIPTION:{escape(description)}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]
    return [piece for line in lines for piece in fold(line)]


def _price(rec: Rec) -> str:
    if rec.billing_period == "yearly":
        return f"${rec.monthly_cost * 12:.2f}/yr"
    return f"${rec.monthly_cost:.2f}"


def billing_events(recs: Iterable[Rec], stamp: str, today: date) -> List[str]:
    """All-day events for each active paid service's next renewal, plus "Cancel by" the day before when the
    optimizer says to cancel. UIDs are the service id plus the date, so a calendar keeps one event per renewal."""
    lines: List[str] = []
    for rec in recs:
        if not rec.is_active or rec.covered_by or rec.monthly_cost <= 0 or not rec.next_renewal:
            continue
        renews = date.fromisoformat(rec.next_renewal)
        lines += _all_day(
            f"wt-renew-{rec.service_id}-{renews.strftime('%Y%m%d')}", stamp, renews,
            f"Renews: {rec.service_name} ({_price(rec)})",
            f"{rec.service_name} renews on {renews.isoformat()}.",
        )
        cancel_by = renews - timedelta(days=1)
        if rec.status in CANCEL_STATUSES and cancel_by >= today:
            lines += _all_day(
                f"wt-cancel-{rec.service_id}-{cancel_by.strftime('%Y%m%d')}", stamp, cancel_by,
                f"Cancel by: {rec.service_name}",
                f"{rec.status_text} Renews {renews.isoformat()} ({_price(rec)}).".strip(),
            )
    return lines
