"""Month-by-month subscription rotation built from the optimizer plan."""

from fastapi import APIRouter, Depends, Query

from penguwatch import clock
from penguwatch.api.deps import get_optimizer, get_repo
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import RotationPlan
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.optimizer.planner import build_rotation

router = APIRouter(tags=["Planner"])


@router.get("/optimizer/rotation", summary="Month-by-month rotation plan", response_model=RotationPlan)
async def get_rotation(
    months: int = Query(6, ge=1, le=12),
    optimizer: SubscriptionOptimizer = Depends(get_optimizer),
    repo: DatabaseRepository = Depends(get_repo),
):
    plan = await optimizer.calculate_plan()
    rotation = build_rotation(plan, clock.today(), months)
    picks = [p for group in rotation.picks.values() for p in group]
    english = await repo.get_en_titles(p.show_id for p in picks)
    for p in picks:
        p.en_title = english.get(p.show_id)
    return rotation
