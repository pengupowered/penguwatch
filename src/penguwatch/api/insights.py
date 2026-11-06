"""Viewing statistics for the Insights tab."""

from fastapi import APIRouter, Depends, Query

from penguwatch.api.deps import get_repo
from penguwatch.db.repository import DatabaseRepository
from penguwatch.insights import api_payload

router = APIRouter(tags=["Insights"])


@router.get("/insights", summary="Viewing insights")
async def insights(months: int = Query(12, ge=1, le=36), repo: DatabaseRepository = Depends(get_repo)):
    data = await repo.get_insights(months)
    names = {s.id: s.name for s in await repo.get_services()}
    return api_payload(data, names)
