"""Notification preview and test endpoints."""

import httpx
from fastapi import APIRouter, Depends, HTTPException

from penguwatch.api.deps import AppContainer, get_container, get_repo, get_optimizer
from penguwatch.db.repository import DatabaseRepository
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.notify import Event, build_events
from penguwatch.security import rate_limit

router = APIRouter(tags=["Notifications"])


@router.get("/notify/preview", summary="Preview the next notifications")
async def notify_preview(
    repo: DatabaseRepository = Depends(get_repo),
    optimizer: SubscriptionOptimizer = Depends(get_optimizer),
    c: AppContainer = Depends(get_container),
):
    """What the next daily check would send (nothing is sent, nothing is marked as sent)."""
    events = await build_events(repo, optimizer)
    return {
        "configured": bool(c.notifier and c.notifier.enabled),
        "kind": c.notifier.kind if c.notifier else None,
        "events": [e.as_dict() for e in events],
    }


@router.post("/notify/test", summary="Send a test notification", dependencies=[Depends(rate_limit("notify-test", 3, 60))])
async def notify_test(c: AppContainer = Depends(get_container)):
    if not c.notifier or not c.notifier.enabled:
        raise HTTPException(status_code=400, detail="Set PENGUWATCH_NOTIFY_URL to enable notifications.")
    try:
        await c.notifier.send(Event(key="test", title="PenguWatch test", message="Notifications are working.", tags=["white_check_mark"]))
    except Exception as e:
        # not the error's text: an HTTP error names the URL, and an ntfy topic URL is as good as a password
        why = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
        raise HTTPException(status_code=502, detail=f"Delivery failed: {why}")
    return {"status": "sent"}
