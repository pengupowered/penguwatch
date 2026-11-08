"""What was watched over the last 7, 30 or 365 days, for the History tab."""

from fastapi import APIRouter, Depends, HTTPException, Query

from penguwatch.api.deps import get_config, get_repo
from penguwatch.config import AppConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.history import PERIODS

router = APIRouter(tags=["History"])


@router.get("/history", summary="Viewing over a rolling period, against the period before")
async def history(
    days: int = Query(30, description="Length of the period in local days, ending today: 7, 30 or 365",
                      json_schema_extra={"enum": list(PERIODS)}),
    include_bulk: bool = Query(False, description="Count episodes marked in bulk (3 or more with one exact timestamp)"),
    repo: DatabaseRepository = Depends(get_repo),
    config: AppConfig = Depends(get_config),
):
    if days not in PERIODS:
        raise HTTPException(status_code=422, detail=f"days must be one of {', '.join(map(str, PERIODS))}")
    return await repo.get_history(days, include_bulk, config.history.bulk_min)
