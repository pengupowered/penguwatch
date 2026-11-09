"""The drivers: which runs in which role, and the library driver's login and sync (/api/drivers/{driver}/...)."""

import dataclasses
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query

from penguwatch.api.deps import AppContainer, _spawn, get_container, get_drivers, get_repo, get_sync
from penguwatch.api.schemas import ConnectRequest, DriverCredentialsRequest
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import DRIVERS
from penguwatch.drivers.base import Capability
from penguwatch.security import rate_limit
from penguwatch.sync.active import ActiveDrivers
from penguwatch.sync.engine import SyncEngine

logger = logging.getLogger("penguwatch.api.drivers")
router = APIRouter(tags=["Drivers"])


def _library(driver: str, drivers: ActiveDrivers, sync: SyncEngine):
    """The library driver named `driver`: 404 when no such driver runs, 409 when it is not the library driver."""
    running = drivers.get(driver)
    if running is None:
        raise HTTPException(status_code=404, detail=f"No driver named {driver!r} is running")
    if running.library is not sync.library:
        raise HTTPException(status_code=409, detail=f"{driver} is not the library driver")
    return sync.library


def _inactive_outbox(rows, library_name: str) -> Dict[str, int]:
    """{driver: rows} still queued for a driver that is not the library driver now (kept, not delivered)."""
    waiting: Dict[str, int] = {}
    for row in rows:
        if row.get("driver") != library_name:
            waiting[row["driver"]] = waiting.get(row["driver"], 0) + 1
    return waiting


def _describe(name: str, cls, running: bool) -> Dict[str, Any]:
    return {
        "name": name, "label": cls.label, "offers": sorted(cap.value for cap in cls.offers), "running": running,
        "key_provider": getattr(cls, "key_provider", name),
        "home_url": getattr(cls, "home_url", None),
        "remote": bool(getattr(cls, "remote", False)) if Capability.LIBRARY_SYNC in cls.offers else None,
        "connect_kind": getattr(cls, "connect_kind", None),
        "extras": list(getattr(cls, "extras", ())),
        "title_urls": dict(getattr(cls, "title_urls", {})),
        # whether Add title takes a pasted link to a title on this service (the catalogue's `parse_link`)
        "takes_links": bool(getattr(cls, "takes_links", False)),
        "rating_sources": [{"id": r.id, "name": r.name, "label": r.label} for r in cls.rating_sources],
        # the credit the service's terms ask for wherever its data is shown (None: none asked)
        "attribution": dataclasses.asdict(cls.attribution) if getattr(cls, "attribution", None) else None,
    }


@router.get("/drivers", summary="The drivers and their roles")
async def list_drivers(c: AppContainer = Depends(get_container), repo: DatabaseRepository = Depends(get_repo)):
    """The driver of each role, and what the page needs to show them without knowing the service: each driver's
    label, capabilities, how its account connects (`connect_kind`, null when there is nothing to connect), the extra
    controls it offers, a link template for a title's page, and its built-in rating sources. `remote` says whether
    the library driver has an account (false: the library lives only in PenguWatch, there is nothing to sync or
    review). `inactive_outbox` counts the queued changes waiting for a driver that is not the library driver now:
    they are kept and go out when that driver is the library driver again."""
    roles = c.config.drivers
    library = c.sync.library if c.sync else None
    waiting = _inactive_outbox(await repo.get_outbox(), roles.library)
    return {
        "library": roles.library, "catalog": roles.catalog, "schedule": roles.schedule, "discovery": roles.discovery,
        "remote": bool(library.remote) if library else None,
        "pull_interval_minutes": library.pull_interval_minutes if library else None,
        "inactive_outbox": waiting,
        "waiting_for_inactive": sum(waiting.values()),
        "drivers": [_describe(name, d, name in c.drivers.by_name) for name, d in sorted(DRIVERS.items())],
    }


@router.get("/drivers/{driver}/status", summary="Login and sync status of the library driver")
async def driver_status(
    driver: str,
    repo: DatabaseRepository = Depends(get_repo),
    drivers: ActiveDrivers = Depends(get_drivers),
    sync: SyncEngine = Depends(get_sync),
):
    library = _library(driver, drivers, sync)
    status = await library.status()
    outbox = [o for o in await repo.get_outbox() if o.get("driver") == library.name]
    incoming = await sync.get_incoming()
    return {
        **status.details,
        "driver": status.driver,
        "label": library.label,
        "remote": library.remote,
        "waiting_for_inactive": sum(_inactive_outbox(await repo.get_outbox(), library.name).values()),
        "authenticated": status.connected,
        "configured": status.configured,
        "auth_state": status.auth_state,
        "auth_message": status.message,
        "review_mode": await sync.review_mode(),
        "awaiting_approval": sum(1 for o in outbox if not o["approved"]),
        "incoming_to_review": len(incoming["entries"]) if library.remote else 0,
        "last_sync": await sync.get_state("last_sync"),
        "last_error": await sync.get_state("last_error"),
        "last_result": sync.last_result,
        "pending_pushes": sum(1 for o in outbox if o["approved"]),
        "failed_pushes": sum(1 for o in outbox if o["approved"] and o.get("attempts")),
    }


@router.get("/drivers/{driver}/diagnose", summary="Diagnose the library driver's connection",
            dependencies=[Depends(rate_limit("driver-diagnose", 6, 60))])
async def driver_diagnose(driver: str, drivers: ActiveDrivers = Depends(get_drivers),
                          sync: SyncEngine = Depends(get_sync)):
    """What the service answers (status and message) to the calls the driver depends on."""
    library = _library(driver, drivers, sync)
    if not hasattr(library, "diagnose"):
        raise HTTPException(status_code=404, detail=f"{driver} has no diagnostics")
    return await library.diagnose()


@router.post("/drivers/{driver}/connect", summary="Connect the library driver's account, step by step")
async def driver_connect(
    driver: str,
    payload: ConnectRequest = ConnectRequest(),
    drivers: ActiveDrivers = Depends(get_drivers),
    sync: SyncEngine = Depends(get_sync),
    c: AppContainer = Depends(get_container),
):
    """Without `state`: the first step (for a device login: a code to enter and the page to enter it on). With the
    `state` a step returned: the next one ("pending" until approved, then "done", which starts a sync)."""
    library = _library(driver, drivers, sync)
    step = await library.connect(payload.state)
    if step.kind == "error":
        raise HTTPException(status_code=400, detail=step.message)
    if step.kind == "done":
        sync.kick.set()  # the worker waits a minute at a time while nothing is connected: wake it now
        _spawn(c.background, sync.sync(trigger="login"))
    return dataclasses.asdict(step)


@router.post("/drivers/{driver}/credentials", summary="Store the library driver's credentials")
async def driver_credentials(driver: str, payload: DriverCredentialsRequest,
                             drivers: ActiveDrivers = Depends(get_drivers), sync: SyncEngine = Depends(get_sync)):
    library = _library(driver, drivers, sync)
    if not hasattr(library, "set_credentials"):
        raise HTTPException(status_code=404, detail=f"{driver} takes no credentials by hand")
    result = await library.set_credentials(payload.model_dump())
    sync.kick.set()  # a pasted login: the worker waits a minute at a time while nothing is connected, wake it now
    return {"status": "success", **result}


@router.post("/drivers/{driver}/disconnect", summary="Disconnect the library driver's account")
async def driver_disconnect(driver: str, drivers: ActiveDrivers = Depends(get_drivers),
                            sync: SyncEngine = Depends(get_sync)):
    await _library(driver, drivers, sync).disconnect()
    return {"status": "disconnected"}


@router.post("/drivers/{driver}/sync", summary="Sync with the library driver now",
             dependencies=[Depends(rate_limit("driver-sync", 6, 60))])
async def driver_sync(
    driver: str,
    full: bool = Query(False),
    drivers: ActiveDrivers = Depends(get_drivers),
    sync: SyncEngine = Depends(get_sync),
) -> Dict[str, Any]:
    """Pushes pending changes and pulls what changed. full=true re-reads the whole library."""
    library = _library(driver, drivers, sync)
    ensure = getattr(drivers.get(driver), "ensure_credentials", None)
    if ensure:
        await ensure()
    if not library.is_connected():
        raise HTTPException(status_code=401, detail=f"{library.label} is not connected.")
    result = await sync.sync(force_full=full, trigger="manual")
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("error"))
    return result
