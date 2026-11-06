"""Up Next queue and the 'tidy up' suggestions."""

from penguwatch import clock
from penguwatch.api.schemas import TidyApplyRequest, TidyDismissRequest
import json
from datetime import timedelta
from typing import Dict, Any
from fastapi import Depends, HTTPException, Query
from penguwatch.interest import score
from penguwatch.models import Show, ShowStatus, Priority, HouseholdTag, MediaType, ShowCreateRequest
from penguwatch.api.deps import AppContainer, get_container, get_repo, get_sync
from penguwatch.db.repository import DatabaseRepository
from penguwatch.sync.engine import SyncEngine
from penguwatch.api.titles import _create_title
from fastapi import APIRouter

router = APIRouter(tags=["Up next"])



@router.get("/up-next", summary="What to watch next")
async def up_next(only_active_services: bool = Query(False), repo: DatabaseRepository = Depends(get_repo)):
    return await repo.get_up_next(only_active_services=only_active_services)

# --- Tidy up: stale "watching" shows and completed shows with new seasons ---

async def _dismissed(repo: DatabaseRepository, kind: str) -> Dict[str, str]:
    raw = await repo.get_state(f"tidy_dismissed_{kind}")
    return json.loads(raw) if raw else {}


def _brief(show: Show) -> Dict[str, Any]:
    out = {k: getattr(show, k) for k in ("id", "title", "year", "poster", "media_type", "status", "network",
                                         "household_tag", "watched_episodes", "aired_episodes", "episodes_left",
                                         "next_episode_air_date", "imdb_rating")}
    if show.en_title:  # only when known, like Show.en_title
        out["en_title"] = show.en_title
    return out


UNASSIGNED_CAP = 60


def _hours_watched(show: Show) -> float:
    return round(show.watched_episodes * (show.runtime_minutes or 0) / 60, 1)


async def _unassigned(repo: DatabaseRepository) -> list:
    """Open titles with no service, most-watched first (they distort the usage numbers most)."""
    # Only these are parsed; the taste profile (whole library) is cached until the next write.
    shows = await repo.get_shows(service_id="unassigned")
    profile = await repo.taste_profile()
    rows = []
    for s in shows:
        if s.service_id or s.status in (ShowStatus.COMPLETED, ShowStatus.DROPPED):
            continue
        rows.append((_hours_watched(s), score(s, profile)[0], s))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2].title))
    services = await repo.get_services()
    configured = {svc.id for svc in services}
    out = []
    by_network: Dict[str, Any] = {}  # many titles share a network: match each distinct one once
    for hours, interest, s in rows[:UNASSIGNED_CAP]:
        if s.network and s.network not in by_network:
            by_network[s.network] = await repo.match_service_by_network(s.network, services=services)
        guess = by_network.get(s.network) if s.network else None
        out.append({
            "id": s.id, "title": s.title, "status": s.status.value, "hours_watched": hours,
            "interest_score": interest,
            "suggested_service_id": guess if guess in configured else None,
            **({"en_title": s.en_title} if s.en_title else {}),
        })
    return out


@router.get("/tidy", summary="Suggestions for tidying the library")
async def get_tidy(months: int = Query(6, ge=1, le=60), repo: DatabaseRepository = Depends(get_repo)):
    dismissed_ret = await _dismissed(repo, "returning")
    dismissed_stale = await _dismissed(repo, "stale")
    returning = []
    for r in await repo.get_returning_shows():
        key = f"{r['show'].id}:{r['first_code']}"
        if key in dismissed_ret:
            continue
        returning.append({**{k: v for k, v in r.items() if k != "show"}, "show": _brief(r["show"]), "key": key})
    stale = []
    for r in await repo.get_stale_watching(months):
        until = dismissed_stale.get(str(r["show"].id))
        if until and until > clock.utc_now().isoformat():
            continue
        stale.append({**{k: v for k, v in r.items() if k != "show"}, "show": _brief(r["show"])})
    return {"months": months, "returning": returning, "stale": stale, "unassigned": await _unassigned(repo)}


@router.post("/tidy/apply", summary="Apply tidy suggestions")
async def tidy_apply(
    payload: TidyApplyRequest,
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
    c: AppContainer = Depends(get_container),
):
    """Moves shows to a status, and/or adds anime sequels (each is a normal library change: reviewed if review mode is on).

    Either {"show_ids": [...], "status": "..."} for one status, or {"moves": [{"show_id", "status"}]} per show."""
    moves = [(m.show_id, m.status) for m in payload.moves]
    moves += [(i, payload.status) for i in payload.show_ids]
    moved = added = 0
    known = {s.id for s in await repo.get_services()}
    for a in payload.assignments:
        if a.service_id not in known:
            raise HTTPException(status_code=422, detail=f"Unknown service: {a.service_id}")
        if not await repo.get_show_by_id(a.show_id):
            raise HTTPException(status_code=404, detail=f"Show not found: {a.show_id}")
    for a in payload.assignments:  # same path as PATCH /api/shows/{id}: sets service_manual
        await repo.update_show_metadata(show_id=a.show_id, service_id=a.service_id)
    for sid, st in moves:
        show = await repo.get_show_by_id(sid)
        if show and show.status != st:
            await sync.update_show_status(sid, st)
            moved += 1
    for sq in payload.sequels:
        parent = await repo.get_show_by_id(sq.parent_id) if sq.parent_id else None
        await _create_title(ShowCreateRequest(
            title=sq.title, ids=sq.ids, media_type=MediaType.ANIME,
            status=payload.status, household_tag=parent.household_tag if parent else HouseholdTag.PERSON1,
            priority=parent.priority if parent else Priority.MEDIUM,
        ), repo, sync, c.background)
        added += 1
    return {"moved": moved, "added": added, "assigned": len(payload.assignments), "review_mode": await sync.review_mode()}


@router.post("/tidy/dismiss", summary="Dismiss tidy suggestions")
async def tidy_dismiss(payload: TidyDismissRequest, repo: DatabaseRepository = Depends(get_repo)):
    """'Not now': hide suggestions. Returning-season hints come back for the *next* new season;
    stale hints come back after the chosen number of months."""
    data = await _dismissed(repo, payload.kind)
    if payload.kind == "returning":
        for key in payload.keys:
            data[str(key)] = clock.utc_now().isoformat()
    else:
        until = (clock.utc_now() + timedelta(days=30 * payload.months)).isoformat()
        for sid in payload.show_ids:
            data[str(sid)] = until
    await repo.set_state(f"tidy_dismissed_{payload.kind}", json.dumps(data))
    return {"dismissed": len(payload.keys or payload.show_ids)}
