"""The drivers component: builds the drivers config.yaml's `drivers:` names (each once, with its stored login) and
the sync engine on the active library driver."""

from penguwatch import telemetry
from penguwatch.components import Component, Context
from penguwatch.sync.active import build_drivers
from penguwatch.sync.engine import SyncEngine


async def register(ctx: Context) -> Component:
    drivers = await build_drivers(ctx.config, ctx.repository)
    engine = SyncEngine(library=drivers.library, repository=ctx.repository, catalogs=drivers.catalogs,
                        schedules=drivers.schedules)
    engine.on_run = telemetry.record_sync_run
    ctx.drivers, ctx.sync = drivers, engine
    return Component("drivers", stop=drivers.aclose)
