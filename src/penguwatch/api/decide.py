"""Tonight's pick."""

from penguwatch import pick
from penguwatch.models import RandomPickFilter
from penguwatch.api.deps import AppContainer, get_container, get_repo, household_ids
from penguwatch.db.repository import DatabaseRepository
from fastapi import APIRouter, Depends

router = APIRouter(tags=["Tonight's pick"])



@router.post("/decide/pick", summary="Pick something to watch tonight")
async def pick_show_to_watch(filters: RandomPickFilter, repo: DatabaseRepository = Depends(get_repo),
                             c: AppContainer = Depends(get_container)):
    """`member`: the titles that person wants; `shared` true: titles two or more people want, false: titles one person
    wants alone (with `member`: that person's own pick); neither: anyone's."""
    members = household_ids(c.config, [filters.member] if filters.member else [], ["body", "member"])
    shows = await repo.get_shows(
        service_id=filters.service_id,
        only_active_services=filters.only_active_services,
        status=filters.status,
        members=members,
        shared=filters.shared,
        media_type=filters.media_type,
    )
    return pick.pick(shows, filters)  # the filtering and weighting are shared with the MCP tool (penguwatch.pick)
