"""The services' "edited by you" marker (`user_edited`, migration 0019): set by the one-time import for the services
that are the user's own (a config.yaml service counts as edited only when its name, aliases, titles
or bundle members differ from the catalogue's entry, compared as a person reads them; a service the catalogue does not
have always counts), and by every add or edit of those fields through the API. A price, renewal, note or active change
never sets it, and nothing clears it."""

import pytest

from penguwatch.catalog import load_catalog
from penguwatch.config import AppConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig
from penguwatch.secrets import SecretStore
from penguwatch.settings.hub import SettingsHub
from penguwatch.settings.importer import refresh_catalogue
from penguwatch.settings.manager import SettingsManager
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

CATALOG = {s.id: s for s in load_catalog()}


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "penguwatch.db"))
    await r.initialize()
    yield r
    await r.close()


async def marks(repo) -> dict:
    db = await repo.read_connection()
    cursor = await db.execute("SELECT id, user_edited FROM services")
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def import_file(repo, tmp_path, raw):
    manager = SettingsManager(AppConfig(), repo, SecretStore(tmp_path / "secrets.json"), SettingsHub(),
                              config_path=str(tmp_path / "config.yaml"), file_raw=raw)
    await manager.load()
    return manager


def catalogue_row(service_id: str) -> dict:
    return CATALOG[service_id].model_dump()


async def test_imported_services_are_edited_only_when_they_differ_from_the_catalogue(repo, tmp_path):
    same = catalogue_row("netflix")
    reordered = catalogue_row("max")
    reordered["aliases"] = [a.upper() + "  " for a in reversed(reordered["aliases"])]  # order, case, spaces
    notes_only = catalogue_row("hidive")
    notes_only.update(notes="my own note", monthly_cost=1.23, is_active=True)
    own_alias = catalogue_row("hulu")
    own_alias["aliases"] = own_alias["aliases"] + ["My Hulu"]
    own_title = catalogue_row("peacock")
    own_title["titles"] = ["A Title Only Here"]
    renamed = catalogue_row("pbs")
    renamed["name"] = "Public Broadcasting Here"
    custom = {"id": "local_tv", "name": "Local TV", "aliases": ["Antenna"]}
    await import_file(repo, tmp_path, {"services": [same, reordered, notes_only, own_alias, own_title, renamed,
                                                    custom]})
    assert await marks(repo) == {"netflix": 0, "max": 0, "hidive": 0, "hulu": 1, "peacock": 1, "pbs": 1,
                                 "local_tv": 1}


async def test_without_services_in_the_file_the_catalogue_seeds_an_empty_table_untouched(repo, tmp_path):
    await import_file(repo, tmp_path, {})
    assert await marks(repo) == {sid: 0 for sid in CATALOG}


async def test_existing_rows_the_catalogue_lacks_are_the_users_and_catalogue_rows_are_not(repo, tmp_path):
    await repo.upsert_service(ServiceConfig(id="mine", name="Mine"))
    await repo.initialize([CATALOG["netflix"]])  # as the old start-up seed left it
    async with repo.write() as db:
        await db.execute("UPDATE services SET user_edited = 0")  # as migration 0019 leaves every row
    await import_file(repo, tmp_path, {})  # no services: in the file, the table is not empty: no seed
    assert await marks(repo) == {"mine": 1, "netflix": 0}


async def test_the_api_marks_an_edit_of_a_refreshed_field_only(repo, tmp_path):
    await import_file(repo, tmp_path, {})
    netflix = (await repo.get_service("netflix")).model_copy()
    await repo.upsert_service(netflix.model_copy(update={"monthly_cost": 99.0, "is_active": True, "notes": "x",
                                                         "renewal_day": 3, "billing_period": "yearly"}))
    await repo.set_service_active("netflix", False)
    assert (await marks(repo))["netflix"] == 0
    await repo.upsert_service(netflix.model_copy(update={"aliases": netflix.aliases + ["Netflix Kids"]}))
    assert (await marks(repo))["netflix"] == 1
    await repo.upsert_service(netflix)  # back to the catalogue's: the marker stays
    assert (await marks(repo))["netflix"] == 1
    await repo.upsert_service(ServiceConfig(id="brand_new", name="Brand New"))
    assert (await marks(repo))["brand_new"] == 1


async def test_the_refresh_touches_only_untouched_services_and_never_the_users_fields(repo, tmp_path):
    await import_file(repo, tmp_path, {})
    async with repo.write() as db:  # an older release's aliases, the user's price and state
        await db.execute("UPDATE services SET aliases_json = '[\"Old\"]', monthly_cost = 1.5, is_active = 1 "
                         "WHERE id IN ('netflix', 'hulu')")
        await db.execute("UPDATE services SET user_edited = 1 WHERE id = 'hulu'")
    assert await refresh_catalogue(repo) == ["netflix"]
    netflix, hulu = await repo.get_service("netflix"), await repo.get_service("hulu")
    assert netflix.aliases == CATALOG["netflix"].aliases and (netflix.monthly_cost, netflix.is_active) == (1.5, True)
    assert hulu.aliases == ["Old"]
    assert await refresh_catalogue(repo) == []  # idempotent


async def test_post_api_services_marks_an_edit_but_not_a_price_change(setup_test_app):
    """Through the real route: POST /api/services (the Services tab's save) sets the marker for a
    change to name, aliases, titles or bundle members, never for a price, renewal, note or billing change."""
    from httpx import ASGITransport, AsyncClient

    from penguwatch.main import app
    repo = setup_test_app
    async with repo.write() as db:
        await db.execute("UPDATE services SET user_edited = 0")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        row = (await ac.get("/api/services")).json()[0]
        priced = await ac.post("/api/services", json={**row, "monthly_cost": 42.5, "notes": "family plan",
                                                      "renewal_day": 9, "billing_period": "yearly"})
        after_price = (await marks(repo))[row["id"]]
        renamed = await ac.post("/api/services", json={**row, "aliases": row["aliases"] + ["Another Name"]})
        after_edit = (await marks(repo))[row["id"]]
    assert priced.status_code == 200 and priced.json()["monthly_cost"] == 42.5 and after_price == 0
    assert renamed.status_code == 200 and after_edit == 1
