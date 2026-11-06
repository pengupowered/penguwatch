"""Drivers: one package per service, each turning that service's answers into the neutral types of drivers.base.

DRIVERS is the built-in registry (no plugin loading: every driver ships with PenguWatch, so the dependency gate sees
everything that runs). config.yaml's `drivers:` picks the driver for each role; `check_roles` refuses, at start-up, a
driver that is not here or that lacks the capability its role needs. This module and the drivers' own packages are
the only places that name a service (tests/test_driver_layer.py checks it).
"""

from typing import Dict, Iterable, List, Sequence

from penguwatch.drivers.base import ROLES, Capability
from penguwatch.drivers.local import LocalDriver
from penguwatch.drivers.simkl import SimklDriver
from penguwatch.drivers.tvmaze import TvmazeDriver

DRIVERS: Dict[str, type] = {driver.name: driver for driver in (SimklDriver, LocalDriver, TvmazeDriver)}

# Roles that may be left empty (`[]`): the feature is then off. A catalogue is how titles get added, so it needs one.
OPTIONAL_ROLES = ("schedule", "discovery")

# What every role uses when config.yaml says nothing
DEFAULT_LIBRARY = SimklDriver.name
DEFAULT_CHAIN: List[str] = [SimklDriver.name]


class DriverConfigError(ValueError):
    """config.yaml's `drivers:` names a driver that is not there or cannot fill the role."""


def check_role(role: str, names: Sequence[str]) -> List[str]:
    """The names, checked: each is a registered driver offering the role's capability. Raises DriverConfigError.
    An empty list is allowed for the roles in OPTIONAL_ROLES (the feature is off) and refused for the others."""
    capability = ROLES[role]
    if not names and role in OPTIONAL_ROLES:
        return []
    if not names:
        why = " (titles are added through the catalogue)" if role == "catalog" else ""
        raise DriverConfigError(
            f"drivers.{role}: name at least one driver{why} (known: {', '.join(sorted(DRIVERS))})")
    out: List[str] = []
    for name in names:
        driver = DRIVERS.get(name)
        if driver is None:
            raise DriverConfigError(
                f"drivers.{role}: unknown driver {name!r} (known: {', '.join(sorted(DRIVERS))})")
        if capability not in driver.offers:
            raise DriverConfigError(
                f"drivers.{role}: the {name} driver does not offer {capability.value} "
                f"(it offers: {', '.join(sorted(c.value for c in driver.offers))})")
        if name not in out:
            out.append(name)
    return out


def identity_providers(library: str, catalogs: Sequence[str]) -> List[str]:
    """The providers whose ids identify a title in the library: the library driver's own id when it has an account
    (its titles are matched by it), then each catalogue driver's. A title added twice, from a search, is found again
    through any of them. A driver names its id in `key_provider` (default: its own name)."""
    def key(name: str) -> str:
        return getattr(DRIVERS[name], "key_provider", name)

    first = [] if not getattr(DRIVERS[library], "remote", True) else [key(library)]
    return list(dict.fromkeys([*first, *(key(n) for n in catalogs)]))


def drivers_with(capability: Capability, names: Iterable[str] = ()) -> List[type]:
    """The registered drivers offering `capability`, those in `names` first in that order, then the rest by name."""
    names = list(names)
    found = [DRIVERS[n] for n in names if n in DRIVERS and capability in DRIVERS[n].offers]
    rest = [d for n, d in sorted(DRIVERS.items()) if n not in names and capability in d.offers]
    return found + rest


__all__ = ["DRIVERS", "DEFAULT_LIBRARY", "DEFAULT_CHAIN", "DriverConfigError", "check_role", "drivers_with", "identity_providers",
           "OPTIONAL_ROLES",
           "Capability"]
