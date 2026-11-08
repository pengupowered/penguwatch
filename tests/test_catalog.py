"""The packaged services catalogue and refresh_untouched."""

import json

import pytest

from penguwatch.catalog import REFRESHED_FIELDS, CatalogError, load_catalog, refresh_untouched
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig


def _svc(**kw):
    base = dict(id="netflix", name="Netflix", monthly_cost=19.99, aliases=["Netflix"], titles=["Seinfeld"])
    return ServiceConfig(**{**base, **kw})


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "catalog.db"))
    await r.initialize([])
    async with r.write() as db:
        # user_edited comes from migration 0019; if the column is missing, this test adds it itself
        cols = [row[1] for row in await (await db.execute("PRAGMA table_info(services)")).fetchall()]
        if "user_edited" not in cols:
            await db.execute("ALTER TABLE services ADD COLUMN user_edited INTEGER NOT NULL DEFAULT 0")
    yield r
    await r.close()


async def _seed(repo, svc, edited=False):
    await repo.upsert_service(svc)
    async with repo.write() as db:
        await db.execute("UPDATE services SET user_edited = ? WHERE id = ?", (1 if edited else 0, svc.id))


async def _refresh(repo, catalog):
    async with repo.write() as db:
        return await refresh_untouched(db, catalog)


def test_packaged_catalogue_loads_and_is_sane():
    cat = load_catalog()
    ids = [s.id for s in cat]
    assert len(ids) == len(set(ids)) >= 14
    assert {"netflix", "disney_hulu_noads"} <= set(ids)
    bundle = next(s for s in cat if s.id == "disney_hulu_noads")
    assert bundle.includes == ["disney_plus", "hulu"] and set(bundle.includes) <= set(ids)
    assert REFRESHED_FIELDS == ("name", "aliases", "titles", "includes")


def test_broken_catalogue_raises_catalog_error(monkeypatch):
    import penguwatch.catalog as c

    class Fake:
        def joinpath(self, _):
            return self

        def read_text(self, **_):
            return "services:\n  - id: a\n    name: A\n  - id: a\n    name: B\n"

    monkeypatch.setattr(c.resources, "files", lambda _: Fake())
    with pytest.raises(CatalogError):
        load_catalog()


@pytest.mark.asyncio
async def test_untouched_service_is_refreshed(repo):
    await _seed(repo, _svc(name="Old", aliases=["old"], titles=[], includes=[]))
    changed = await _refresh(repo, [_svc(name="Netflix", aliases=["Netflix", "The CW"], titles=["Seinfeld"])])
    assert changed == ["netflix"]
    got = await repo.get_service("netflix")
    assert (got.name, got.aliases, got.titles) == ("Netflix", ["Netflix", "The CW"], ["Seinfeld"])


@pytest.mark.asyncio
async def test_bundle_members_are_refreshed(repo):
    await _seed(repo, _svc(id="bun", name="Bun", includes=["a"]))
    await _refresh(repo, [_svc(id="bun", name="Bun", includes=["a", "b"])])
    assert (await repo.get_service("bun")).includes == ["a", "b"]


@pytest.mark.asyncio
async def test_edited_service_is_not_refreshed(repo):
    await _seed(repo, _svc(name="Mine", aliases=["mine"]), edited=True)
    assert await _refresh(repo, [_svc(name="Netflix", aliases=["Netflix"])]) == []
    got = await repo.get_service("netflix")
    assert (got.name, got.aliases) == ("Mine", ["mine"])


@pytest.mark.asyncio
async def test_price_renewal_notes_and_active_never_change(repo):
    await _seed(repo, _svc(name="Old", monthly_cost=5.5, is_active=True, renewal_day=9, renewal_date="2026-12-01",
                           notes="mine", billing_period="yearly"))
    await _refresh(repo, [_svc(name="Netflix", monthly_cost=99.0, is_active=False, renewal_day=1, notes="cat",
                               aliases=["x"])])
    got = await repo.get_service("netflix")
    assert got.name == "Netflix" and got.aliases == ["x"]
    assert (got.monthly_cost, got.is_active, got.renewal_day, got.renewal_date, got.notes, got.billing_period) == \
        (5.5, True, 9, "2026-12-01", "mine", "yearly")
    async with repo.write() as db:
        n = (await (await db.execute("SELECT COUNT(*) FROM service_price_history WHERE service_id = 'netflix'")).fetchone())[0]
        edited = (await (await db.execute("SELECT user_edited FROM services WHERE id = 'netflix'")).fetchone())[0]
    assert n == 1 and edited == 0


@pytest.mark.asyncio
async def test_second_run_is_a_no_op(repo):
    await _seed(repo, _svc(name="Old", aliases=[]))
    cat = [_svc()]
    assert await _refresh(repo, cat) == ["netflix"]
    assert await _refresh(repo, cat) == []


@pytest.mark.asyncio
async def test_service_missing_from_table_is_not_added_and_unknown_row_is_left_alone(repo):
    await _seed(repo, _svc(id="mine", name="Mine"))
    changed = await _refresh(repo, [_svc(id="netflix")])
    assert changed == []
    assert [s.id for s in await repo.get_services()] == ["mine"]
    assert (await repo.get_service("mine")).name == "Mine"
