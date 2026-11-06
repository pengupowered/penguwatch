"""Price history: migration 0006, recording on every price change, the API, and the focus advice paid count."""

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.config import OptimizerConfig
from penguwatch.db.migrations import run_migrations
from penguwatch.db.repository import DatabaseRepository
from penguwatch.main import app
from penguwatch.models import ServiceConfig
from penguwatch.optimizer.engine import SubscriptionOptimizer


def svc(sid="net", cost=10.0, active=True, **kw):
    return ServiceConfig(id=sid, name=sid.title(), monthly_cost=cost, is_active=active, **kw)


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "prices.db"))
    yield r
    await r.close()


def prices(history):
    return [h["monthly_cost"] for h in history]


async def test_migration_0006_creates_table_and_seeds_existing_services():
    async with aiosqlite.connect(":memory:") as conn:
        await run_migrations(conn)
        cur = await conn.execute("PRAGMA table_info(service_price_history)")
        assert {r[1] for r in await cur.fetchall()} == {"id", "service_id", "monthly_cost", "effective_from"}
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        assert "idx_price_history_service" in {r[0] for r in await cur.fetchall()}
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("CREATE TABLE services (id TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_cost REAL "
                           "DEFAULT 0.0, is_active INTEGER DEFAULT 0, renewal_day INTEGER, renewal_date TEXT, "
                           "aliases_json TEXT DEFAULT '[]', notes TEXT)")
        await conn.execute("INSERT INTO services (id, name, monthly_cost) VALUES ('old', 'Old', 7.5), ('free', 'Free', 0)")
        await conn.commit()
        await run_migrations(conn)
        cur = await conn.execute("SELECT service_id, monthly_cost, effective_from FROM service_price_history ORDER BY service_id")
        rows = [tuple(r) for r in await cur.fetchall()]
        assert [(r[0], r[1]) for r in rows] == [("free", 0.0), ("old", 7.5)]
        assert all(r[2] for r in rows)


async def test_new_services_are_seeded_with_one_row_and_reseeding_adds_none(repo):
    await repo.initialize([svc("a", 5.0), svc("b", 0.0)])
    assert prices(await repo.get_price_history("a")) == [5.0]
    assert prices(await repo.get_price_history("b")) == [0.0]
    await repo.initialize([svc("a", 99.0), svc("b", 0.0)])  # seeding never changes a stored price
    assert prices(await repo.get_price_history("a")) == [5.0]


async def test_price_changes_are_recorded_newest_first_and_unchanged_saves_are_not(repo):
    await repo.initialize([svc("a", 5.0)])
    await repo.upsert_service(svc("a", 5.0, notes="same price"))
    assert prices(await repo.get_price_history("a")) == [5.0]
    await repo.upsert_service(svc("a", 6.5))
    await repo.upsert_service(svc("a", 6.5, renewal_day=3))
    await repo.upsert_service(svc("a", 4.0))
    history = await repo.get_price_history("a")
    assert prices(history) == [4.0, 6.5, 5.0]
    assert [h["effective_from"] for h in history] == sorted((h["effective_from"] for h in history), reverse=True)


async def test_a_service_added_by_the_user_starts_its_history(repo):
    await repo.initialize([])
    await repo.upsert_service(svc("mine", 3.0))
    assert prices(await repo.get_price_history("mine")) == [3.0]
    assert await repo.get_price_history("nobody") == []


async def test_prices_endpoint(container):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        body = {"id": "netflix", "name": "Netflix", "monthly_cost": 17.99, "is_active": False}
        assert (await ac.post("/api/services", json=body)).status_code == 200
        res = await ac.get("/api/services/netflix/prices")
        assert res.status_code == 200
        assert [h["monthly_cost"] for h in res.json()] == [17.99, 15.49]
        assert set(res.json()[0]) == {"monthly_cost", "effective_from"}
        assert (await ac.get("/api/services/nope/prices")).status_code == 404
        # the services payload is unchanged
        assert "price_history" not in (await ac.get("/api/services")).json()[0]


# --- optimizer: the target limit counts paid services only ---

async def _advice(tmp_path, services, max_active):
    repo = DatabaseRepository(str(tmp_path / "advice.db"))
    await repo.initialize(services)
    cfg = OptimizerConfig(max_active_services=max_active)
    plan = await SubscriptionOptimizer(repository=repo, config=cfg).calculate_plan()
    await repo.close()
    return plan.rotation_advice


async def test_free_and_covered_services_do_not_count_towards_the_target_limit(tmp_path):
    services = [
        svc("paid", 10.0), svc("free1", 0.0), svc("free2", 0.0),
        svc("combo", 20.0, includes=["inc"]), svc("inc", 8.0, active=False),
    ]
    advice = await _advice(tmp_path, services, max_active=2)
    assert not any("exceeding" in line for line in advice)  # 2 paid (paid, combo); 3 free/covered are not counted
    assert any(line.startswith("Current focus") for line in advice)


async def test_exceeding_the_target_counts_paid_services(tmp_path):
    services = [svc("a", 10.0), svc("b", 12.0), svc("c", 5.0), svc("free", 0.0)]
    advice = await _advice(tmp_path, services, max_active=2)
    line = next(line for line in advice if "exceeding" in line)
    assert "3 paid active services" in line and "target limit of 2" in line
