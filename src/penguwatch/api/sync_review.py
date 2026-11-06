"""Review mode: outgoing/incoming changes waiting for a decision."""

from penguwatch.api.schemas import (
    IncomingResolveRequest, OutgoingApproveRequest, OutgoingDiscardRequest, ReviewModeRequest,
)
from penguwatch.sync.ops import describe
from penguwatch.api.deps import get_repo, get_sync
from penguwatch.db.repository import DatabaseRepository
from penguwatch.sync.engine import SyncEngine
from penguwatch.sync.push import DiscardRefused
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["Sync review"])


_INTERNAL = ("payload", "operation")


@router.get("/sync/outbox", summary="Changes waiting to go to the library")
async def get_outbox(repo: DatabaseRepository = Depends(get_repo)):
    return [{k: v for k, v in o.items() if k not in _INTERNAL} for o in await repo.get_outbox()]


@router.get("/sync/review", summary="Sync review state")
async def get_sync_review(repo: DatabaseRepository = Depends(get_repo), sync: SyncEngine = Depends(get_sync)):
    """Everything waiting for a decision: local changes not yet sent, and library changes not yet applied."""
    outgoing = []
    for o in await repo.get_outbox():
        row = {k: v for k, v in o.items() if k not in _INTERNAL}
        row["summary"] = row.get("summary") or describe(o["operation"], sync.library.label)
        row["discard_blocked"] = sync.discard_refusal(o)  # None: the row can be discarded
        outgoing.append(row)
    incoming = await sync.get_incoming()
    # The page shows a title by its English name when it has one (as everywhere else): take it from the library for
    # titles already here (also for entries staged before the name was recorded with them)
    for e in incoming["entries"]:
        show = await repo.get_show_by_id(e["show_id"]) if e.get("show_id") else None
        if show is not None and show.en_title:
            e["en_title"] = show.en_title
    return {
        "review_mode": await sync.review_mode(),
        "outgoing": outgoing,
        "incoming": incoming["entries"],
        "incoming_checked_at": incoming["at"],
    }


@router.post("/sync/review/mode", summary="Turn review mode on or off")
async def set_review_mode(payload: ReviewModeRequest, repo: DatabaseRepository = Depends(get_repo)):
    await repo.set_state("sync_review_mode", "on" if payload.enabled else "off")
    return {"review_mode": payload.enabled}


@router.post("/sync/outgoing/approve", summary="Approve outgoing changes")
async def approve_outgoing(
    payload: OutgoingApproveRequest = OutgoingApproveRequest(),
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
):
    n = await sync.approve_outgoing(payload.ids)
    sent = await sync.flush_outbox() if sync.library.is_connected() else 0
    remaining = [o for o in await repo.get_outbox() if o["approved"]]
    return {"approved": n, "sent": sent, "still_queued": len(remaining), "last_error": await sync.get_state("last_error")}


@router.post("/sync/outgoing/discard", summary="Discard outgoing changes",
             description="Drops the changes and restores those titles to what the library has. Refused (409) for rows "
                         "of a driver that is not the connected library driver; `restore: false` drops them anyway, "
                         "without restoring anything.")
async def discard_outgoing(payload: OutgoingDiscardRequest, sync: SyncEngine = Depends(get_sync)):
    try:
        return await sync.discard_outgoing(payload.ids, restore=payload.restore)
    except DiscardRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/sync/incoming/resolve", summary="Accept or reject incoming changes")
async def resolve_incoming(
    payload: IncomingResolveRequest,
    sync: SyncEngine = Depends(get_sync),
):
    """accept=true: take the library's version. accept=false: keep ours and send it there."""
    res = await sync.resolve_incoming(payload.keys, payload.accept)
    if not payload.accept and sync.library.is_connected():
        res["sent"] = await sync.flush_outbox()
    return res


@router.post("/sync/outbox/retry", summary="Retry failed outbox items")
async def retry_outbox(repo: DatabaseRepository = Depends(get_repo), sync: SyncEngine = Depends(get_sync)):
    await repo.release_outbox_backoff()
    sent = await sync.flush_outbox()
    return {"sent": sent, "remaining": sum(1 for o in await repo.get_outbox() if o["approved"])}


@router.delete("/sync/outbox", summary="Clear the outbox")
async def clear_outbox(repo: DatabaseRepository = Depends(get_repo)):
    return {"cleared": await repo.clear_outbox()}
