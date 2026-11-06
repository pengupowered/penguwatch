"""The driver layer (docs/drivers.md): the registry, the neutral types, and the `drivers:` roles checked at start-up."""

import pytest
from pydantic import ValidationError

from penguwatch import main
from penguwatch.config import AppConfig, DriversConfig
from penguwatch.drivers import DEFAULT_LIBRARY, DRIVERS, DriverConfigError, check_role, drivers_with
from penguwatch.drivers.base import (
    MEDIA_TYPES, ROLES, Capability, SyncOp, normal_id, normal_ids,
)


# --- the registry ---------------------------------------------------------------------------------------------------

def test_every_registered_driver_declares_what_the_registry_reads():
    for name, driver in DRIVERS.items():
        assert driver.name == name
        assert driver.label and isinstance(driver.label, str)
        assert driver.offers and all(isinstance(c, Capability) for c in driver.offers)
        assert isinstance(driver.secret_state_keys, tuple)
        assert isinstance(driver.rating_sources, tuple)


def test_the_default_library_driver_is_registered_and_offers_every_role():
    driver = DRIVERS[DEFAULT_LIBRARY]
    assert set(ROLES.values()) <= driver.offers


def test_drivers_with_puts_the_named_ones_first():
    assert drivers_with(Capability.CATALOG, [DEFAULT_LIBRARY])[0] is DRIVERS[DEFAULT_LIBRARY]
    assert all(Capability.CATALOG in d.offers for d in drivers_with(Capability.CATALOG))


def test_media_types_match_the_models():
    from penguwatch.models import MediaType
    assert set(MEDIA_TYPES) == {m.value for m in MediaType}


# --- config: drivers ------------------------------------------------------------------------------------------------

def test_without_a_drivers_section_every_role_uses_the_default():
    cfg = AppConfig().drivers
    assert cfg.library == DEFAULT_LIBRARY
    assert cfg.catalog == cfg.schedule == cfg.discovery == [DEFAULT_LIBRARY]


def test_a_role_may_name_one_driver_without_a_list_and_duplicates_collapse():
    cfg = DriversConfig(catalog=DEFAULT_LIBRARY, schedule=[DEFAULT_LIBRARY, DEFAULT_LIBRARY])
    assert cfg.catalog == [DEFAULT_LIBRARY] and cfg.schedule == [DEFAULT_LIBRARY]


@pytest.mark.parametrize("section,needle", [
    ({"library": "nosuch"}, "drivers.library: unknown driver 'nosuch'"),
    ({"catalog": ["nosuch"]}, "drivers.catalog: unknown driver 'nosuch'"),
    ({"catalog": []}, "drivers.catalog: name at least one driver (titles are added through the catalogue)"),
    ({"library": "simkl", "catalog": ["local"]}, "drivers.catalog: the local driver does not offer catalog"),
    ({"discovery": ["local"]}, "drivers.discovery: the local driver does not offer discovery"),
    ({"surprise": "x"}, "surprise"),
])
def test_an_unknown_driver_or_role_is_refused_with_a_message_naming_it(section, needle):
    with pytest.raises(ValidationError) as err:
        AppConfig(drivers=section)
    assert needle in str(err.value)


def test_a_driver_without_the_roles_capability_is_refused(monkeypatch):
    class CatalogOnly:
        name = "catalogonly"
        label = "Catalog only"
        offers = frozenset({Capability.CATALOG})
        options_model = None
        rating_sources = ()
        secret_state_keys = ()

    monkeypatch.setitem(DRIVERS, "catalogonly", CatalogOnly)
    assert check_role("catalog", ["catalogonly", DEFAULT_LIBRARY]) == ["catalogonly", DEFAULT_LIBRARY]
    with pytest.raises(DriverConfigError, match="drivers.library: the catalogonly driver does not offer library_sync"):
        check_role("library", ["catalogonly"])
    with pytest.raises(ValidationError, match="does not offer schedule"):
        AppConfig(drivers={"schedule": ["catalogonly"]})


def test_schedule_and_discovery_may_be_empty_which_turns_them_off():
    cfg = DriversConfig(schedule=[], discovery=[])
    assert cfg.schedule == [] and cfg.discovery == [] and cfg.catalog == [DEFAULT_LIBRARY]
    assert check_role("schedule", []) == [] and check_role("discovery", []) == []
    with pytest.raises(DriverConfigError, match="catalog"):
        check_role("catalog", [])
    with pytest.raises(DriverConfigError, match="library"):
        check_role("library", [])


def test_the_local_library_driver_is_registered_and_offers_only_library_sync():
    assert DriversConfig(library="local").library == "local"
    local = DRIVERS["local"]
    assert local.offers == frozenset({Capability.LIBRARY_SYNC}) and local.remote is False
    assert DRIVERS[DEFAULT_LIBRARY].remote is True  # the default stays an account driver


def test_titles_are_identified_by_the_catalogue_ids_when_the_library_has_no_account():
    from penguwatch.drivers import identity_providers
    assert identity_providers("simkl", ["simkl"]) == ["simkl"]
    assert identity_providers("local", ["simkl"]) == ["simkl"]  # not "local": it identifies nothing a search returns


async def test_an_unknown_driver_in_config_yaml_stops_the_start(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("drivers:\n  library: nosuch\n", encoding="utf-8")
    monkeypatch.setenv("PENGUWATCH_CONFIG", str(cfg))
    monkeypatch.setenv("PENGUWATCH_DB_PATH", str(tmp_path / "never.db"))
    with pytest.raises(ValidationError, match="drivers.library: unknown driver 'nosuch'"):
        async with main.lifespan(main.app):
            pass  # pragma: no cover
    assert not (tmp_path / "never.db").exists()  # nothing was opened


# --- neutral types --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    (123, 123), ("123", 123), (" tt0903747 ", "tt0903747"), ("0123", "0123"), (0, None), ("", None), (None, None),
    (True, None), (-4, None), (1.5, None),
])
def test_normal_id(raw, want):
    assert normal_id(raw) == want


def test_normal_ids_keeps_usable_values_under_lower_case_names():
    assert normal_ids({"IMDB": "tt1", "tmdb": "55", "tvdb": None, "": 3, "slug": "", 7: 1}) == {"imdb": "tt1", "tmdb": 55}


def test_a_sync_op_survives_its_json_form():
    op = SyncOp(kind="watched", media_type="show", ids={"imdb": "tt1", "tmdb": 2}, episodes=[(1, 2), (1, 3)],
                watched_at="2026-10-10T12:00:00+00:00")
    again = SyncOp.from_json(op.to_json(), row_id=9)
    assert again.to_json() == op.to_json() and again.row_id == 9
    assert "rating" not in op.to_json() and "native" not in op.to_json()


# --- the guard: nothing outside a driver names its service ---------------------------------------------------------

import ast  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src" / "penguwatch"
VENDOR = "simkl"
# Where the service may be named, and why
VENDOR_PLACES = {
    "drivers/simkl/": "the driver itself",
    "drivers/__init__.py": "the registry imports every driver",
    "db/migrations/": "migrations move the data the pre-driver code left under the service's name",
}
# Identifiers that keep the name by decision: the built-in rating source's storage column (the owner kept the
# columns as storage; generic code reaches them through the drivers' rating sources)
VENDOR_IDENTIFIERS = ("simkl_rating",)


def _python_modules():
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if not any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in VENDOR_PLACES):
            yield rel, path


def vendor_mentions(text: str):
    """(line number, line) of each mention of the service outside the allowed identifiers."""
    found = []
    for n, line in enumerate(text.splitlines(), start=1):
        stripped = line
        for ident in VENDOR_IDENTIFIERS:
            stripped = re.sub(rf"\b{ident}\b", "", stripped)
        if VENDOR in stripped.lower():
            found.append((n, line.strip()))
    return found


def test_nothing_outside_the_driver_names_its_service():
    offenders = [f"{rel}:{n}: {line}" for rel, path in _python_modules()
                 for n, line in vendor_mentions(path.read_text(encoding="utf-8"))]
    assert offenders == []


def test_nothing_outside_the_driver_imports_it():
    offenders = []
    for rel, path in _python_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            offenders += [f"{rel}:{node.lineno} imports {m}" for m in names if m.startswith("penguwatch.drivers.simkl")]
    assert offenders == []


def test_the_guard_finds_a_mention_and_an_import():
    """The checks above are only as good as their matching: a mention in a comment or a string, any case, counts;
    the storage column alone does not."""
    assert vendor_mentions("x = 1  # pushed to SIMKL") == [(1, "x = 1  # pushed to SIMKL")]
    assert vendor_mentions('key = "driver.simkl.token"') != []
    assert vendor_mentions("show.simkl_rating or show.imdb_rating") == []
    assert vendor_mentions("show.simkl_rating_x") != []
    assert list(_python_modules()) and all(not rel.startswith("drivers/simkl/") for rel, _ in _python_modules())
