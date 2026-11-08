"""Pure shaping of the History tab: what was watched over the last 7, 30 or 365 days, compared with the period before.

No I/O. The repository (db/history.py) hands over the raw rows; this module decides which local day, weekday and hour
each belongs to, which are bulk marks, and builds the payload.

Rules (all in the household's configured time zone, never UTC substrings):
- A period is `days` whole local days ending with today (today included); the previous period is the `days` before it.
- A stamp is UTC text in one of several shapes ("2026-03-23T04:21:46Z", "2026-11-07T01:55:06.093604+00:00", a space
  instead of the T, no offset, or date-only "2026-03-23"). A date-only stamp has no time of day: it belongs to its own
  date, is never in the hour-of-day counts and is never a bulk mark.
- 3 or more episodes with the identical stored stamp (which has a time) are a bulk mark ("mark the whole season",
  "first N", an import). They do not say when anyone watched, so unless `include_bulk` they count in no total, chart or
  list, only in `bulk_episodes` / `bulk_groups`.
- Hours use the show's runtime (45 min if unknown; 110 for a movie), like Insights. Movies have no episode rows and
  count once at their last-watched stamp.
- Weekday 0 is Sunday (the same as the Insights tab).
"""

import json
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Tuple

PERIODS = (7, 30, 365)
BULK_MIN = 3  # episodes sharing one exact stamp: the default of the setting history.bulk_min
DAYS_PER_MONTH = 30.44  # as the optimizer prices a window
TOP = 10
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Window:
    days: int
    today: date
    tz: tzinfo
    first_day: date = field(init=False)  # the period's first local day
    prev_first_day: date = field(init=False)  # the previous period's

    def __post_init__(self) -> None:  # plain attributes: period_of() runs once per watched episode
        object.__setattr__(self, "first_day", self.today - timedelta(days=self.days - 1))
        object.__setattr__(self, "prev_first_day", self.first_day - timedelta(days=self.days))

    def at_midnight(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=self.tz)

    def query_bounds(self) -> Tuple[str, str]:
        """Text bounds for the stored stamps, `lo <= stamp < hi`, wide enough for any offset: every stamp of a local day
        in the two periods starts with a UTC date from the day before the previous period to the day after today. They
        are compared as text so the (is_watched, watched_at) index can serve the range; the exact test is the local day."""
        return (self.prev_first_day - timedelta(days=1)).isoformat(), (self.today + timedelta(days=2)).isoformat()

    def period_of(self, day: date) -> Optional[int]:
        """0 for the current period, 1 for the previous, None for outside both."""
        if self.first_day <= day <= self.today:
            return 0
        if self.prev_first_day <= day < self.first_day:
            return 1
        return None


def zone_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or ("UTC" if tz is timezone.utc else str(tz))


def parse_stamp(text: Optional[str], tz: tzinfo) -> Optional[Tuple[date, Optional[int]]]:
    """(local day, local hour or None for a date-only stamp), or None when the text is not a date."""
    if not text:
        return None
    t = text.strip()
    try:
        if len(t) == 10:
            return date.fromisoformat(t), None
        dt = datetime.fromisoformat(t[:-1] + "+00:00" if t[-1] in "Zz" else t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored stamps are UTC
    local = dt.astimezone(tz)
    return local.date(), local.hour


def _has_time(text: str) -> bool:
    return len(text.strip()) > 10


def _genres(raw: Optional[str]) -> List[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [g for g in value if isinstance(g, str) and g] if isinstance(value, list) else []


def _hours(minutes: int) -> float:
    return round(minutes / 60.0, 1)


class _Totals:
    def __init__(self) -> None:
        self.minutes = 0
        self.episodes = 0
        self.movies = 0
        self.started = 0
        self.finished = 0
        self.days: set = set()

    def add(self, minutes: int, day: date, movie: bool) -> None:
        self.minutes += minutes
        self.days.add(day)
        if movie:
            self.movies += 1
        else:
            self.episodes += 1

    def out(self) -> Dict[str, Any]:
        return {"hours": _hours(self.minutes), "episodes": self.episodes, "movies": self.movies,
                "titles_started": self.started, "titles_finished": self.finished, "active_days": len(self.days)}


def _series_keys(window: Window) -> Tuple[str, List[Dict[str, Any]]]:
    """The empty buckets: one per local day for 7 and 30 days, one per local month for a year (13 at most, the first
    and last partial unless the period happens to start on the 1st / end on the month's last day)."""
    first, last = window.first_day, window.today
    if window.days <= 30:
        return "day", [{"key": (first + timedelta(days=i)).isoformat(), "start": (first + timedelta(days=i)).isoformat(),
                        "label": (first + timedelta(days=i)).strftime("%a %d").replace(" 0", " "), "partial": False}
                       for i in range(window.days)]
    buckets, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        starts_late = (y, m) == (first.year, first.month) and first.day != 1
        ends_early = (y, m) == (last.year, last.month) and last != nxt - timedelta(days=1)
        buckets.append({"key": f"{y:04d}-{m:02d}", "start": date(y, m, 1).isoformat(), "label": MONTH_NAMES[m - 1],
                        "partial": starts_late or ends_early})
        y, m = nxt.year, nxt.month
    return "month", buckets


def _price_per_day(points: List[Tuple[date, float]], day: date) -> float:
    days = [d for d, _ in points]
    return points[max(bisect_right(days, day) - 1, 0)][1] / DAYS_PER_MONTH


def _services(window: Window, minutes: Dict[str, int], items: Dict[str, int], services: List[Dict[str, Any]],
              prices: Dict[str, List[Tuple[str, float]]]) -> List[Dict[str, Any]]:
    """Hours per service; for a service that is active now, what it cost over the period and per hour watched.

    PenguWatch records no past subscriptions, only whether a service is active now and every price it has had. So the
    cost assumes an active service was paid for every day of the period, at the price in effect on that day (the first
    known price before it was recorded). A service that is not active has no cost here."""
    by_id = {s["id"]: s for s in services}
    out = []
    for sid in sorted(set(minutes) | {s["id"] for s in services if s["is_active"] and s["monthly_cost"] > 0}):
        svc = by_id.get(sid)
        mins = minutes.get(sid, 0)
        row = {"service_id": sid, "service": svc["name"] if svc else ("Unassigned" if not sid else sid),
               "hours": _hours(mins), "episodes": items.get(sid, 0), "active": bool(svc and svc["is_active"]),
               "cost": None, "cost_per_hour": None}
        if svc and svc["is_active"] and svc["monthly_cost"] > 0:
            points = sorted((got[0], c) for t, c in prices.get(sid, []) if (got := parse_stamp(t, window.tz)))
            points = points or [(date.min, svc["monthly_cost"])]
            cost = sum(_price_per_day(points, window.first_day + timedelta(days=i)) for i in range(window.days))
            row["cost"] = round(cost, 2)
            if mins > 0:
                row["cost_per_hour"] = round(cost / (mins / 60.0), 2)
        out.append(row)
    out.sort(key=lambda r: (-r["hours"], r["service"]))
    return out


def build_history(raw: Dict[str, Any], window: Window, include_bulk: bool = False,
                  bulk_min: int = BULK_MIN) -> Dict[str, Any]:
    """Turns the rows of HistoryMixin._history_rows() into the /api/history payload (posters are added by the caller).

    raw: episodes [(show_id, watched_at, minutes)], movies [(show_id, last_watched_at, minutes)], older {ids of the
    titles with a watched episode before both periods}, shows {show_id: {title, service_id, genres_json}}, finished [last_watched_at],
    services [{id, name, monthly_cost, is_active}], prices {service_id: [(effective_from, monthly_cost)]}."""
    tz = window.tz
    parsed: Dict[str, Optional[Tuple[date, Optional[int]]]] = {}

    def stamp(text: str):
        if text not in parsed:
            parsed[text] = parse_stamp(text, tz)
        return parsed[text]

    shared = Counter(t for _, t, _ in raw["episodes"] if t and _has_time(t))
    bulk_texts = {t for t, n in shared.items() if n >= bulk_min}

    totals = (_Totals(), _Totals())
    show_min: Dict[int, int] = {}
    show_items: Dict[int, int] = {}
    genre_min: Dict[str, int] = {}
    service_min: Dict[str, int] = {}
    service_items: Dict[str, int] = {}
    weekdays = [0] * 7
    hours_of_day = [0] * 24
    unit, buckets = _series_keys(window)
    by_key = {b["key"]: b for b in buckets}
    for b in buckets:
        b.update(minutes=0, episodes=0, movies=0)
    bulk_episodes, bulk_groups = [0], set()

    def count(show_id: int, text: str, minutes: int, movie: bool, bulk: bool) -> None:
        got = stamp(text)
        if got is None:
            return
        day, hour = got
        period = window.period_of(day)
        if period is None:
            return
        if bulk and period == 0:
            bulk_episodes[0] += 1
            bulk_groups.add(text)
        if bulk and not include_bulk:
            return
        totals[period].add(minutes, day, movie)
        if period:
            return
        show_min[show_id] = show_min.get(show_id, 0) + minutes
        show_items[show_id] = show_items.get(show_id, 0) + 1
        info = raw["shows"].get(show_id) or {}
        sid = info.get("service_id") or ""
        service_min[sid] = service_min.get(sid, 0) + minutes
        service_items[sid] = service_items.get(sid, 0) + 1
        for g in _genres(info.get("genres_json")):
            genre_min[g] = genre_min.get(g, 0) + minutes
        weekdays[day.isoweekday() % 7] += 1
        if hour is not None:
            hours_of_day[hour] += 1
        key = day.isoformat() if unit == "day" else f"{day.year:04d}-{day.month:02d}"
        b = by_key[key]
        b["minutes"] += minutes
        b["movies" if movie else "episodes"] += 1

    older = raw["older"]
    first_stamp: Dict[int, str] = {}  # a title's earliest stamp, unless it has an older one outside the two periods
    for show_id, text, minutes in raw["episodes"]:
        if text:
            if show_id not in older and text < first_stamp.get(show_id, "~"):
                first_stamp[show_id] = text
            count(show_id, text, minutes, False, text in bulk_texts)
    for show_id, text, minutes in raw["movies"]:
        if text:
            count(show_id, text, minutes, True, False)

    for show_id, text in first_stamp.items():
        got = stamp(text) if text else None
        period = window.period_of(got[0]) if got else None
        if period is not None and (include_bulk or text not in bulk_texts):
            totals[period].started += 1
    for text in raw["finished"]:
        got = stamp(text) if text else None
        period = window.period_of(got[0]) if got else None
        if period is not None:
            totals[period].finished += 1

    top = sorted(show_min, key=lambda i: (-show_min[i], (raw["shows"].get(i) or {}).get("title") or "", i))[:TOP]
    return {
        "period": {
            "days": window.days, "tz": zone_name(tz),
            "start": window.at_midnight(window.first_day).isoformat(),
            "end": window.at_midnight(window.today + timedelta(days=1)).isoformat(),  # exclusive
            "previous_start": window.at_midnight(window.prev_first_day).isoformat(),
            "first_day": window.first_day.isoformat(), "last_day": window.today.isoformat(),
        },
        "include_bulk": include_bulk,
        "bulk_episodes": bulk_episodes[0],
        "bulk_groups": len(bulk_groups),
        "weekday_start": "sunday",  # weekdays[0] is Sunday, as on the Insights tab
        "totals": {"current": totals[0].out(), "previous": totals[1].out()},
        "series": {"unit": unit, "buckets": [
            {"key": b["key"], "start": b["start"], "label": b["label"], "partial": b["partial"],
             "hours": _hours(b["minutes"]), "episodes": b["episodes"], "movies": b["movies"]} for b in buckets]},
        "top_shows": [{"id": i, "title": (raw["shows"].get(i) or {}).get("title") or "", "hours": _hours(show_min[i]),
                       "episodes": show_items[i], "poster": None} for i in top],
        "by_service": _services(window, service_min, service_items, raw["services"], raw["prices"]),
        "genres": [{"genre": g, "hours": _hours(m)}
                   for g, m in sorted(genre_min.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]],
        "weekdays": weekdays,
        "hours_of_day": hours_of_day,
    }
