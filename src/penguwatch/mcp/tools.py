"""The MCP tools: read-only questions about the library, answered by the functions the API routes call.

A tool is `Tool(name, description, model, handler)`: `model` is a pydantic model of its arguments (it validates them
and gives the tool's JSON Schema), `handler` takes the app's AppContainer and the validated arguments and returns plain
data. It raises ToolFailure for a failure of its own (an unknown title: the caller sees `isError` and the sentence).
Nothing here imports the web framework, and no tool writes: picking is a pure draw, nothing is recorded, marked watched
or synced.

Nothing a tool returns holds a secret, and no defence relies on scrubbing text: titles and services are summarized field
by field (never dumped whole), sync_status is built from a whitelist (penguwatch.sync.summary) from stored state only and
carries no free text from a driver or a service (the last error is a category and a fixed sentence), and no tool reads
the configuration's credentials, the access token, or the notification or webhook addresses. The titles, overviews and
names a library holds come from third-party catalogues and from the user: they are data for the assistant to read, not
instructions. Times are ISO 8601 in the household time zone (clock.tz()); air dates without a time stay dates.

Adding a tool: write the model and handler and put the Tool in TOOLS.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from penguwatch import clock, pick
from penguwatch.api.deps import AppContainer
from penguwatch.db.episodes import format_episode_code
from penguwatch.history import PERIODS
from penguwatch.models import RandomPickFilter, Show, ShowStatus
from penguwatch.optimizer.planner import build_rotation
from penguwatch.sync.summary import library_summary


class ToolFailure(Exception):
    """A tool's own failure, told to the caller as a plain sentence (isError)."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    model: Type[BaseModel]
    handler: Callable[[AppContainer, Any], Awaitable[Any]]

    @property
    def input_schema(self) -> Dict[str, Any]:
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema


DEFAULT_LIMIT = 10
MAX_LIMIT = 100
UP_NEXT_MAX = 60  # the API's own cap (repository.get_up_next)


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _int(default, low: int, high: int, description: str):
    """An integer argument: a real integer (not true or "5"), inside its bounds."""
    return Field(default, strict=True, ge=low, le=high, description=description)


def _limit(default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT):
    return _int(default, 1, maximum, f"How many to return (default {default}, at most {maximum}).")


# --- times ----------------------------------------------------------------------------------------------

def local_time(value: Optional[str]) -> Optional[str]:
    """A stored timestamp as ISO 8601 in the household zone; a plain date (an air date) is left as it is."""
    if not value or "T" not in value:
        return value
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(clock.tz()).isoformat(timespec="seconds")


# --- how a title is shown ---------------------------------------------------------------------------------------------

def _who(c: AppContainer, show: Show) -> str:
    return c.config.household.labels().get(show.household_tag.value, show.household_tag.value)


def brief(c: AppContainer, show: Show) -> Dict[str, Any]:
    """One line of a list: what a title is and how far along."""
    out: Dict[str, Any] = {
        "id": show.id, "title": show.title, "year": show.year, "type": show.media_type.value,
        "status": show.status.value, "for": _who(c, show), "service_id": show.service_id,
        "watched": show.watched_episodes, "aired": show.aired_episodes, "total": show.total_episodes,
        "episodes_left": show.episodes_left, "imdb": show.imdb_rating, "my_rating": show.user_rating,
    }
    if show.en_title:
        out["en_title"] = show.en_title
    return {k: v for k, v in out.items() if v is not None}


EPISODES_NOTE = (
    "`next_to_watch` is the first unwatched episode that has already aired (what to press play on; its `air_date` is in "
    "the past); `next_new_episode` is the next episode that has NOT aired yet, with its air date. They are different "
    "episodes and either may be missing.")


def _episode(code: Optional[str], title: Optional[str], air_date: Optional[str]) -> Optional[Dict[str, Any]]:
    if not code:
        return None
    return {k: v for k, v in {"code": code, "title": title, "air_date": local_time(air_date)}.items() if v is not None}


async def next_episodes(c: AppContainer, show: Show) -> Dict[str, Any]:
    """`next_to_watch` and `next_new_episode` of a show (EPISODES_NOTE), the same in every tool."""
    out: Dict[str, Any] = {}
    match = re.fullmatch(r"S(\d+)E(\d+)", (show.next_to_watch or "").upper())
    if match:
        season, number = int(match.group(1)), int(match.group(2))
        found = next((e for e in await c.repository.get_episodes(show.id)
                      if e.season == season and e.number == number), None)
        out["next_to_watch"] = _episode(show.next_to_watch, found.title if found else None,
                                        found.air_date if found else None)
    elif show.next_to_watch:
        out["next_to_watch"] = _episode(show.next_to_watch, None, None)
    new = _episode(format_episode_code(show.next_episode_season, show.next_episode_number), show.next_episode_title,
                   show.next_episode_air_date)
    if new:
        out["next_new_episode"] = new
    return {k: v for k, v in out.items() if v}


# --- up_next -----------------------------------------------------------------------------------------------------------

class UpNextArgs(Args):
    limit: int = _limit(10, UP_NEXT_MAX)
    only_active_services: bool = Field(False, description="Only titles on a service that is subscribed now.")


async def _up_next(c: AppContainer, a: UpNextArgs):
    # every show in progress, so `total` is the real count (the API's own list stops at 60)
    entries = await c.repository.get_up_next(only_active_services=a.only_active_services, limit=100_000)
    shows = []
    for e in entries[:a.limit]:
        nxt = e.get("up_next") or {}
        row = {"id": e["id"], "title": e["title"], "type": getattr(e["media_type"], "value", e["media_type"]),
               "service": e.get("service_name"), "service_active": e.get("service_active"),
               "for": c.config.household.labels().get(getattr(e["household_tag"], "value", e["household_tag"])),
               "watched": e["watched_episodes"], "aired": e["aired_episodes"], "episodes_left": e["episodes_left"],
               "next_to_watch": _episode(nxt.get("code"), nxt.get("title"), nxt.get("air_date")),
               "next_new_episode": _episode(format_episode_code(e.get("next_episode_season"), e.get("next_episode_number")),
                                            e.get("next_episode_title"), e.get("next_episode_air_date")),
               "ready_to_binge": e["is_ready_to_binge"]}
        if e.get("en_title"):
            row["en_title"] = e["en_title"]
        shows.append({k: v for k, v in row.items() if v is not None})
    return {"total": len(entries), "shown": len(shows), "up_next": shows}


# --- tonights_pick -----------------------------------------------------------------------------------------------------

class PickArgs(Args, RandomPickFilter):
    """Tonight's Pick's own filter (the same fields as POST /api/decide/pick)."""


async def _tonights_pick(c: AppContainer, a: PickArgs):
    shows = await c.repository.get_shows(
        service_id=a.service_id, only_active_services=a.only_active_services, status=a.status,
        household_tag=a.household_tag, media_type=a.media_type)
    drawn = pick.pick(shows, a)
    chosen = drawn["result"]
    if chosen is None:
        return {"pick": None, "matching": 0, "message": drawn["message"]}
    return {"pick": {**brief(c, chosen), **await next_episodes(c, chosen)}, "matching": drawn["total_matching"], "message": drawn["message"],
            "note": "A weighted random draw (priority, who wants it, rating, in progress); asking again may give another title."}


# --- search_library ----------------------------------------------------------------------------------------------------

class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=100, description="Part of a title (any language the library knows it in).")
    status: Optional[ShowStatus] = Field(None, description="Only titles with this status.")
    limit: int = _limit()


async def _search_library(c: AppContainer, a: SearchArgs):
    found = await c.repository.get_shows(search_query=a.query, status=a.status)
    return {"matches": len(found), "shown": min(len(found), a.limit), "titles": [brief(c, s) for s in found[:a.limit]]}


# --- title -------------------------------------------------------------------------------------------------------------

class TitleArgs(Args):
    id: Optional[int] = Field(None, strict=True, ge=1, le=2**63 - 1, description="The title's id (from another tool's output).")
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="Or part of its name.")

    @model_validator(mode="after")
    def _one_of(self):
        if (self.id is None) == (self.name is None):
            raise ValueError("give either id or name")
        return self


async def _title(c: AppContainer, a: TitleArgs):
    repo = c.repository
    others: List[Show] = []
    if a.id is not None:
        show = await repo.get_show_by_id(a.id)
    else:
        found = await repo.get_shows(search_query=a.name, sort_by="title")
        wanted = a.name.casefold()
        show = next((s for s in found if wanted in (s.title.casefold(), (s.en_title or "").casefold())), None) \
            or (found[0] if found else None)
        others = [s for s in found if show and s.id != show.id][:5]
    if show is None:
        raise ToolFailure(f"No title with id {a.id} in the library." if a.id is not None
                          else f"No title in the library matches {a.name!r}.")
    service = await repo.get_service(show.service_id) if show.service_id else None
    out = brief(c, show)
    out.update({
        "genres": show.genres, "network": show.network, "priority": show.priority.value,
        "overview": (show.overview or "")[:600] or None, "runtime_minutes": show.runtime_minutes,
        "is_ended": show.is_ended, "ready_to_binge": show.is_ready_to_binge, "last_watched": show.last_watched,
        "last_watched_at": local_time(show.last_watched_at), **await next_episodes(c, show),
        "ratings": {k: v for k, v in {"imdb": show.imdb_rating, "imdb_votes": show.imdb_votes,
                                      "library_service": show.simkl_rating, "mine": show.user_rating,
                                      **{k: r.value for k, r in show.ratings.items()}}.items() if v is not None},
        "service": ({"id": service.id, "name": service.name, "monthly_cost": service.monthly_cost,
                     "subscribed": service.is_active} if service else None),
    })
    if others:
        out["other_matches"] = [{"id": s.id, "title": s.title} for s in others]
    return {k: v for k, v in out.items() if v not in (None, [])}


# --- schedule ----------------------------------------------------------------------------------------------------------

class ScheduleArgs(Args):
    days_ahead: int = _int(14, 1, 90, "How many days ahead to look (default 14, at most 90).")
    limit: int = _limit(30)


async def _schedule(c: AppContainer, a: ScheduleArgs):
    if not c.drivers.schedules:
        return {"enabled": False, "message": "No schedule driver is set (drivers.schedule is empty), so there is no air schedule.",
                "episodes": []}
    items = await c.repository.get_upcoming_schedule(days_ahead=a.days_ahead)
    rows = []
    for it in items[:a.limit]:
        row = {"id": it["id"], "title": it["title"], "season": it.get("next_episode_season"),
               "episode": it.get("next_episode_number"), "episode_title": it.get("next_episode_title"),
               "air": local_time(it.get("next_episode_air_date")), "service": it.get("service_name") or it.get("network"),
               "service_active": bool(it.get("is_service_active")),
               "for": c.config.household.labels().get(it.get("household_tag"))}
        if it.get("en_title"):
            row["en_title"] = it["en_title"]
        rows.append({k: v for k, v in row.items() if v is not None})
    return {"enabled": True, "days_ahead": a.days_ahead, "total": len(items), "shown": len(rows), "episodes": rows}


# --- optimizer ---------------------------------------------------------------------------------------------------------

class OptimizerArgs(Args):
    rotation_months: int = _int(6, 0, 12, "Months of the month-by-month rotation plan (0: leave it out).")
    limit: int = _limit(20, 40)


async def _optimizer(c: AppContainer, a: OptimizerArgs):
    plan = await c.optimizer.calculate_plan()
    services = [{
        "id": r.service_id, "name": r.service_name, "active": r.is_active, "monthly_cost": r.monthly_cost,
        "status": r.status, "text": r.status_text, "action": r.action, "reason": r.recommendation_reason,
        "hours_backlog": r.hours_backlog, "titles": r.shows_count, "ready_to_binge": r.ready_to_binge_count,
        "next_renewal": r.next_renewal, "decide_by": r.decide_by, "cost_per_hour": r.cost_per_hour,
        "watching": r.watching_titles[:5],
    } for r in plan.service_rankings[:a.limit]]
    out: Dict[str, Any] = {
        "monthly_spend": plan.current_monthly_spend, "if_all_active": plan.all_services_spend,
        "monthly_savings": plan.monthly_savings, "annual_savings": plan.annual_savings,
        "savable_monthly_cost": plan.savable_monthly_cost, "max_active_services": plan.max_active_services,
        "next_decision": plan.next_decision, "advice": plan.rotation_advice,
        "bundle_suggestions": [b.text for b in plan.bundle_suggestions], "unassigned_titles": plan.unassigned_titles,
        "services": [{k: v for k, v in s.items() if v not in (None, "", [])} for s in services],
    }
    if a.rotation_months:
        rotation = build_rotation(plan, clock.today(), a.rotation_months)
        out["rotation"] = {
            "months": rotation.months, "planned_avg_monthly_spend": rotation.summary.planned_avg_monthly_spend,
            "monthly_difference": rotation.summary.monthly_difference, "cancel_now": rotation.summary.cancel_now,
            "notes": rotation.notes,
            "services": [{"id": s.service_id, "name": s.name,
                          "plan": [f"{g.kind}: months {g.start_month + 1}-{g.end_month + 1}" for g in s.segments]}
                         for s in rotation.services],
            "picks": {month: [p.title for p in picks[:5]] for month, picks in rotation.picks.items()},
        }
    return out


# --- sync_status -------------------------------------------------------------------------------------------------------

class SyncStatusArgs(Args):
    pass


async def _sync_status(c: AppContainer, a: SyncStatusArgs):
    summary = await library_summary(c.sync, c.repository)
    summary["last_sync"] = local_time(summary["last_sync"])
    roles = c.config.drivers
    summary["drivers"] = {"library": roles.library, "catalog": roles.catalog, "schedule": roles.schedule,
                          "discovery": roles.discovery}
    return {k: v for k, v in summary.items() if v is not None}


# --- history -----------------------------------------------------------------------------------------------------------

HISTORY_TOP = 5  # top shows and genres in the answer (the History tab shows 10)


class HistoryArgs(Args):
    days: int = Field(30, strict=True, description="Length of the period in local days, ending today: 7, 30 or 365.",
                      json_schema_extra={"enum": list(PERIODS)})
    include_bulk: bool = Field(False, strict=True, description="Also count episodes marked in bulk (several with one exact "
                                                               "timestamp; 3 or more unless the History bulk setting says "
                                                               "otherwise); by default they are left out.")

    @field_validator("days")
    @classmethod
    def _period(cls, value: int) -> int:
        if value not in PERIODS:
            raise ValueError(f"days must be one of {', '.join(map(str, PERIODS))}")
        return value


async def _history(c: AppContainer, a: HistoryArgs):
    # the payload of GET /api/history (the same repository call), cut down: no poster data, no weekday or hour counts,
    # and the per-bucket series only for the 7 daily bars
    # history.bulk_min read from the live config at each call: a change on the Settings tab applies at once
    h = await c.repository.get_history(a.days, a.include_bulk, c.config.history.bulk_min)
    period = {k: h["period"][k] for k in ("days", "tz", "start", "end", "previous_start")}
    out: Dict[str, Any] = {
        "period": period, "note": "Times are ISO 8601 in the household time zone; `end` is exclusive.",
        "include_bulk": h["include_bulk"], "bulk_episodes": h["bulk_episodes"], "bulk_groups": h["bulk_groups"],
        "totals": h["totals"],
        "top_shows": [{k: t[k] for k in ("id", "title", "hours", "episodes")} for t in h["top_shows"][:HISTORY_TOP]],
        "by_service": [{k: r[k] for k in ("service", "hours", "episodes", "active", "cost", "cost_per_hour")}
                       for r in h["by_service"]],
        "genres": h["genres"][:HISTORY_TOP],
    }
    if a.days == 7:
        out["daily"] = [{k: b[k] for k in ("key", "hours", "episodes", "movies")} for b in h["series"]["buckets"]]
    return out


TOOLS: List[Tool] = [
    Tool("up_next",
         "What to watch next: the shows in progress, titles with episodes ready first. " + EPISODES_NOTE +
         " Use for 'what's up next?' or 'what can I continue watching?'.", UpNextArgs, _up_next),
    Tool("tonights_pick",
         "Suggest one title to watch tonight: a weighted random draw among open titles that have something to watch "
         "(priority, who wants it, rating and progress count). Optional filters narrow it. Use for 'what should we watch "
         "tonight?'. Nothing is recorded. " + EPISODES_NOTE, PickArgs, _tonights_pick),
    Tool("search_library",
         "Find titles in the user's own library by part of the name (not the online catalogue). Returns id, status, "
         "progress and service for each. Use to look something up or to get an id for the `title` tool.",
         SearchArgs, _search_library),
    Tool("title",
         "Everything the library knows about one title: progress, next episode, ratings, genres, overview and the "
         "service it is on. Give its id (from another tool) or part of its name. " + EPISODES_NOTE, TitleArgs, _title),
    Tool("schedule",
         "Upcoming episode air dates for library titles, soonest first, with the service each is on. Use for 'what airs "
         "this week?'. Empty when no schedule driver is set.", ScheduleArgs, _schedule),
    Tool("optimizer",
         "The subscription optimizer as the Optimizer tab shows it: monthly spend, what could be saved, advice, which "
         "service to keep, start or cancel and why, and the month-by-month rotation. Use for questions about streaming "
         "costs and which services to pay for.", OptimizerArgs, _optimizer),
    Tool("sync_status",
         "How the sync with the library driver is doing: connected or not, last sync and last error, "
         "and how many changes wait to be sent or reviewed. Reads stored state only (no call to the service) and never shows "
         "credentials or the service's own error text. Use for 'is the sync working?'.",
         SyncStatusArgs, _sync_status),
    Tool("history",
         "What the household actually watched over the last 7, 30 or 365 days against the period before: hours, "
         "episodes, movies, titles started and finished, active days, the top shows, hours and cost per hour by service, "
         "and the top genres (a daily series for 7 days only). Episodes marked in bulk (3 or more with one exact "
         "timestamp) are left out unless include_bulk is true, and their count is reported. Use for 'what did we watch "
         "this month?' or 'is a service worth its price?'; use up_next instead for what to watch next.",
         HistoryArgs, _history),
]
