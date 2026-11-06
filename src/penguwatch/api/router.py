"""API assembly: mounts the routers listed in penguwatch.api.ROUTERS under /api."""

from fastapi import APIRouter

from penguwatch.api import ROUTERS

router = APIRouter(prefix="/api")
for feature_router in ROUTERS:
    router.include_router(feature_router)
