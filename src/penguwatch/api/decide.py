"""Tonight's pick."""

import random
from penguwatch.models import (
    ShowStatus,
    Priority,
    HouseholdTag,
    MediaType,
    RandomPickFilter,
)
from penguwatch.api.deps import get_repo
from penguwatch.db.repository import DatabaseRepository
from fastapi import APIRouter, Depends

router = APIRouter(tags=["Tonight's pick"])



@router.post("/decide/pick", summary="Pick something to watch tonight")
async def pick_show_to_watch(filters: RandomPickFilter, repo: DatabaseRepository = Depends(get_repo)):
    shows = await repo.get_shows(
        service_id=filters.service_id,
        only_active_services=filters.only_active_services,
        status=filters.status,
        household_tag=filters.household_tag,
        media_type=filters.media_type,
    )
    candidates = [s for s in shows if s.status not in (ShowStatus.COMPLETED, ShowStatus.DROPPED)]
    # Shows that are caught up have nothing to watch tonight
    candidates = [s for s in candidates if s.media_type == MediaType.MOVIE or s.episodes_left > 0 or s.aired_episodes == 0]
    if filters.min_rating:
        candidates = [s for s in candidates if (s.imdb_rating or 0.0) >= filters.min_rating]
    if filters.ready_to_binge_only:
        candidates = [s for s in candidates if s.is_ready_to_binge]
    if filters.genre:
        g_lower = filters.genre.lower()
        candidates = [s for s in candidates if any(g_lower in g.lower() for g in s.genres)]
    if not candidates:
        return {"result": None, "message": "No shows found matching the selected criteria."}

    weights = []
    for s in candidates:
        w = 1.0
        if s.household_tag == HouseholdTag.BOTH:
            w *= 3.0
        if s.priority == Priority.HIGH:
            w *= 3.0
        elif s.priority == Priority.MEDIUM:
            w *= 1.5
        if s.status == ShowStatus.WATCHING:
            w *= 2.0
        if s.imdb_rating:
            w *= 1.0 + max(0.0, s.imdb_rating - 7.0) * 0.25
        weights.append(w)

    selected = random.choices(candidates, weights=weights, k=1)[0]
    return {"result": selected, "total_matching": len(candidates), "message": f"Tonight's Pick: {selected.title}!"}
