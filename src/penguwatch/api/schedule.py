"""Upcoming air schedule and the iCal feed."""

from typing import Optional
from datetime import (
    timedelta,
    date,
)
from fastapi import Depends, Query
from fastapi.responses import Response
from penguwatch import clock
from penguwatch.api import ical
from penguwatch.api.deps import get_drivers, get_optimizer, get_repo
from penguwatch.db.repository import DatabaseRepository
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.sync.active import ActiveDrivers
from fastapi import APIRouter

router = APIRouter(tags=["Calendar"])



@router.get("/calendar/upcoming", summary="Upcoming episodes")
async def get_upcoming_calendar(days_ahead: int = Query(30), repo: DatabaseRepository = Depends(get_repo),
                                drivers: ActiveDrivers = Depends(get_drivers)):
    """Upcoming episodes of library titles; empty when no schedule driver is set (drivers.schedule: [])."""
    if not drivers.schedules:
        return []
    return await repo.get_upcoming_schedule(days_ahead=days_ahead)


def _ics_escape(text: str) -> str:
    return ical.escape(text)


def feed_title(title: str, en_title: Optional[str]) -> str:
    """English first with the stored title in brackets: "Frieren: Beyond Journey's End (Sousou no Frieren)"."""
    return f"{en_title} ({title})" if en_title and en_title != title else title


@router.get("/calendar.ics", summary="Calendar feed (iCal)")
async def calendar_ics(
    days_ahead: int = Query(60),
    repo: DatabaseRepository = Depends(get_repo),
    optimizer: SubscriptionOptimizer = Depends(get_optimizer),
    drivers: ActiveDrivers = Depends(get_drivers),
):
    """Subscribable iCal feed (add it to phone/Google/Outlook calendars).

    Episodes follow `days_ahead`; every active paid service also adds its next renewal, and a "Cancel by"
    reminder the day before when the optimizer says to cancel it."""
    items = await repo.get_upcoming_schedule(days_ahead=days_ahead) if drivers.schedules else []
    stamp = clock.utc_now().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PenguWatch//Air Schedule//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:PenguWatch – Upcoming Episodes",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    for it in items:
        d = (it.get("next_episode_air_date") or "")[:10]
        if len(d) != 10:
            continue
        start = d.replace("-", "")
        end = (date.fromisoformat(d) + timedelta(days=1)).strftime("%Y%m%d")
        s, n = it.get("next_episode_season") or 1, it.get("next_episode_number") or 1
        summary = f"{feed_title(it['title'], it.get('en_title'))} S{s:02d}E{n:02d}"
        if it.get("next_episode_title"):
            summary += f" – {it['next_episode_title']}"
        where = it.get("service_name") or it.get("network") or ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:wt-{it['id']}-{s}-{n}@penguwatch",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape('Streaming on ' + where if where else '')}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    plan = await optimizer.calculate_plan()
    lines += ical.billing_events(plan.service_rankings, stamp, clock.today())
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8")
