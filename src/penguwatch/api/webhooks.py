"""Event webhooks: what is configured (never its URL or secret), the events that exist, and a test delivery."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from penguwatch.api.deps import AppContainer, get_container
from penguwatch.events import EVENT_TYPES, PAYLOAD_VERSION
from penguwatch.security import rate_limit

router = APIRouter(tags=["Webhooks"])


class WebhookStatus(BaseModel):
    name: str
    host: str
    events: List[str]
    signed: bool
    state: str  # ok, idle (nothing delivered yet), failing or disabled
    disabled_reason: Optional[str] = None
    queued: int
    delivered: int
    failed: int
    dropped: int
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None


class WebhookEvents(BaseModel):
    payload_version: int
    events: Dict[str, str]


class WebhookTestResult(BaseModel):
    delivered: bool
    error: Optional[str] = None


@router.get("/webhooks", response_model=List[WebhookStatus], summary="List the configured webhooks and their delivery state")
async def list_webhooks(c: AppContainer = Depends(get_container)):
    """Name, host, events and counters of each webhook from config.yaml. The URL and the secret are never returned."""
    return c.webhooks.describe() if c.webhooks else []


@router.get("/webhooks/events", response_model=WebhookEvents, summary="The events a webhook can subscribe to")
async def webhook_events():
    return {"payload_version": PAYLOAD_VERSION, "events": EVENT_TYPES}


@router.post("/webhooks/{name}/test", response_model=WebhookTestResult, summary="Send a test event to one webhook",
             dependencies=[Depends(rate_limit("webhook-test", 6, 60))])
async def test_webhook(name: str = Path(max_length=40), c: AppContainer = Depends(get_container)) -> Dict[str, Any]:
    """Delivers a `webhook.test` event right now (no retries) and says whether the receiver accepted it."""
    hook = next((h for h in (c.webhooks.hooks if c.webhooks else []) if h.name == name), None)
    if hook is None:
        raise HTTPException(status_code=404, detail="No webhook with that name")
    if hook.disabled_reason:
        raise HTTPException(status_code=409, detail=f"This webhook is disabled: {hook.disabled_reason}")
    ok = await c.webhooks.send_test(hook)
    return {"delivered": ok, "error": None if ok else hook.last_error}
