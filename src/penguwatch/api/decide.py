"""Tonight's pick."""

from penguwatch import pick
from penguwatch.models import RandomPickFilter
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
    return pick.pick(shows, filters)  # the filtering and weighting are shared with the MCP tool (penguwatch.pick)
