"""Building the SIMKL driver and the sync engine on it, the way the drivers component does, around a test client."""

from penguwatch.drivers.simkl.options import SimklOptions
from penguwatch.drivers.simkl.service import SimklService
from penguwatch.sync.active import ActiveDrivers
from penguwatch.sync.engine import SyncEngine


def simkl_service(client, repository, options=None) -> SimklService:
    return SimklService(client, repository, options or SimklOptions())


def simkl_engine(client, repository, options=None) -> SyncEngine:
    """The sync engine with SIMKL in every role (the default configuration)."""
    service = simkl_service(client, repository, options)
    engine = SyncEngine(library=service.library, repository=repository, catalogs=[service.catalog],
                        schedules=[service.schedule])
    engine.service = service  # for tests that reach the driver
    return engine


def simkl_drivers(engine: SyncEngine) -> ActiveDrivers:
    service = engine.service
    return ActiveDrivers(by_name={"simkl": service}, library=service.library, catalogs=[service.catalog],
                         schedules=[service.schedule], discoveries=[service.discovery])
