"""Shared fixtures: a fully wired AppContainer installed on the real FastAPI app."""

import pytest

from penguwatch.api.deps import AppContainer, install
from penguwatch.config import AppConfig, ServiceConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.main import app, security_config
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.drivers.simkl.client import SimklClient
from tests.simkl_helpers import simkl_drivers, simkl_engine


@pytest.fixture
async def container(tmp_path):
    db_file = str(tmp_path / "test_api.db")
    config = AppConfig()
    config.database.db_path = db_file
    config.services = [
        ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, is_active=True, aliases=["Apple"]),
        ServiceConfig(id="netflix", name="Netflix", monthly_cost=15.49, is_active=False),
    ]

    repo = DatabaseRepository(db_file)
    await repo.initialize(config.services)

    client = SimklClient(client_id="mock_id", access_token="mock_token")
    sync_svc = simkl_engine(client, repo)
    optimizer = SubscriptionOptimizer(repository=repo, config=config.optimizer)

    c = AppContainer(
        config=config, repository=repo, drivers=simkl_drivers(sync_svc), sync=sync_svc,
        optimizer=optimizer, security=security_config, notifier=None,
    )
    install(app, c)

    yield c

    await repo.close()
    if hasattr(app.state, "container"):
        del app.state.container
