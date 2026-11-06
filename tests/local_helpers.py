"""Building the local library driver and the sync engine on it, the way the drivers component does."""

from penguwatch.drivers.local import LocalService
from penguwatch.sync.active import ActiveDrivers
from penguwatch.sync.engine import SyncEngine


def local_engine(repository, catalogs=(), schedules=()) -> SyncEngine:
    """The sync engine with the local library driver (catalogues and schedules as given)."""
    service = LocalService()
    engine = SyncEngine(library=service.library, repository=repository, catalogs=list(catalogs),
                        schedules=list(schedules))
    engine.service = service
    return engine


def local_drivers(engine: SyncEngine, catalogs=(), schedules=(), discoveries=()) -> ActiveDrivers:
    return ActiveDrivers(by_name={"local": engine.service}, library=engine.library, catalogs=list(catalogs),
                         schedules=list(schedules), discoveries=list(discoveries))
