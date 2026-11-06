"""Shared API plumbing: the AppContainer, its request-scoped providers and small request helpers."""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from fastapi import Depends, FastAPI, HTTPException, Request
from penguwatch.models import Show
from penguwatch.db.repository import DatabaseRepository
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.config import AppConfig
from penguwatch.sync.active import ActiveDrivers
from penguwatch.sync.engine import SyncEngine


@dataclass
class AppContainer:
    config: AppConfig
    repository: DatabaseRepository
    drivers: ActiveDrivers
    sync: SyncEngine
    optimizer: SubscriptionOptimizer
    security: Any = None  # SecurityConfig, for the System tab
    notifier: Any = None  # notify.Notifier
    webhooks: Any = None  # webhooks.WebhookDispatcher
    background: set = field(default_factory=set)


def install(app: FastAPI, container: AppContainer) -> None:
    app.state.container = container


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_repo(c: AppContainer = Depends(get_container)) -> DatabaseRepository:
    return c.repository


def get_sync(c: AppContainer = Depends(get_container)) -> SyncEngine:
    return c.sync


def get_drivers(c: AppContainer = Depends(get_container)) -> ActiveDrivers:
    return c.drivers


def get_optimizer(c: AppContainer = Depends(get_container)) -> SubscriptionOptimizer:
    return c.optimizer


def get_config(c: AppContainer = Depends(get_container)) -> AppConfig:
    return c.config


def _spawn(background: set, coro):
    """Fire-and-forget task that is kept referenced until done."""
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)
    return task


def unprocessable(loc, message: str) -> HTTPException:
    """A 422 in FastAPI's own shape (`detail` is a list of {type, loc, msg}), for checks the models cannot make."""
    return HTTPException(status_code=422, detail=[{"type": "value_error", "loc": list(loc), "msg": message}])


async def _show_or_404(repo: DatabaseRepository, show_id: int) -> Show:
    show = await repo.get_show_by_id(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    return show
