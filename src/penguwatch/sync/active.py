"""The running drivers: each driver config.yaml's `drivers:` names, built once with its stored login, and the
capability objects each role uses."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from penguwatch import __version__, telemetry
from penguwatch.drivers import DRIVERS


@dataclass
class ActiveDrivers:
    """The running drivers, by name, and the capability objects each role uses (in config order)."""

    by_name: Dict[str, Any] = field(default_factory=dict)
    library: Any = None
    catalogs: List[Any] = field(default_factory=list)
    schedules: List[Any] = field(default_factory=list)
    discoveries: List[Any] = field(default_factory=list)

    def get(self, name: str):
        return self.by_name.get(name)

    async def aclose(self) -> None:
        for driver in self.by_name.values():
            await driver.aclose()


LAST_ACCOUNT_KEY = "library.last_account"


async def remember_account(repository, library: str) -> None:
    """Records the library driver in app_state when it has an account, so that while the library is local the engine
    still knows which account its edits belong to (sync.push.PushMixin._queue_for_account). A local library leaves
    the record as it was."""
    if getattr(DRIVERS[library], "remote", False):
        await repository.set_state(LAST_ACCOUNT_KEY, library)


async def build_drivers(config, repository) -> ActiveDrivers:
    roles = config.drivers
    names = list(dict.fromkeys([roles.library, *roles.catalog, *roles.schedule, *roles.discovery]))
    active = ActiveDrivers()
    for name in names:
        cls = DRIVERS[name]
        options = config.driver_options(name)
        active.by_name[name] = await cls.build(repository, options, app_version=__version__,
                                               on_request=telemetry.observe_upstream)
    active.library = active.by_name[roles.library].library
    await remember_account(repository, roles.library)
    active.catalogs = [active.by_name[n].catalog for n in roles.catalog]
    active.schedules = [active.by_name[n].schedule for n in roles.schedule]
    active.discoveries = [active.by_name[n].discovery for n in roles.discovery]
    return active
