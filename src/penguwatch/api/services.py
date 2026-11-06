"""Streaming services and the rotation optimizer plan."""

from penguwatch.api.schemas import ServiceToggleRequest
from fastapi import Depends, HTTPException
from typing import List
from penguwatch.models import ServiceConfig, OptimizerPlan
from penguwatch.api.deps import get_repo, get_optimizer
from penguwatch.db.repository import DatabaseRepository
from penguwatch.optimizer.engine import SubscriptionOptimizer
from fastapi import APIRouter

router = APIRouter(tags=["Services"])



@router.get("/services", summary="List streaming services", response_model=List[ServiceConfig])
async def list_services(repo: DatabaseRepository = Depends(get_repo)):
    return await repo.get_services()


@router.post("/services", summary="Create or update a streaming service", response_model=ServiceConfig)
async def save_service(service: ServiceConfig, repo: DatabaseRepository = Depends(get_repo)):
    await repo.upsert_service(service)
    return await repo.get_service(service.id)


@router.get("/services/{service_id}/prices", summary="Price history of a service")
async def service_price_history(service_id: str, repo: DatabaseRepository = Depends(get_repo)):
    """Every price the service has had, newest first: [{monthly_cost, effective_from}]."""
    if not await repo.get_service(service_id):
        raise HTTPException(status_code=404, detail="Service not found")
    return await repo.get_price_history(service_id)


@router.put("/services/{service_id}/toggle", summary="Switch a service on or off")
async def toggle_service(
    service_id: str,
    payload: ServiceToggleRequest,
    repo: DatabaseRepository = Depends(get_repo),
):
    if not await repo.get_service(service_id):
        raise HTTPException(status_code=404, detail="Service not found")
    # Omitted keeps the stored value; an explicit null clears it.
    sent = payload.model_fields_set
    await repo.set_service_active(
        service_id,
        is_active=payload.is_active,
        renewal_day=payload.renewal_day,
        renewal_date=payload.renewal_date,
        clear_renewal_day="renewal_day" in sent and payload.renewal_day is None,
        clear_renewal_date="renewal_date" in sent and payload.renewal_date is None,
    )
    return await repo.get_service(service_id)


# --- Optimizer ---

@router.get("/optimizer/plan", summary="Subscription optimizer plan", response_model=OptimizerPlan)
async def get_optimizer_plan(optimizer: SubscriptionOptimizer = Depends(get_optimizer)):
    return await optimizer.calculate_plan()
