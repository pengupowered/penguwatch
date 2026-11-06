"""AppContainer, dependency providers and install()."""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from penguwatch.api.deps import (
    AppContainer, get_config, get_container, get_drivers, get_optimizer, get_repo, get_sync,
    install,
)
from penguwatch.db.repository import DatabaseRepository


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(db_path=str(tmp_path / "container.db"))
    await r.initialize(default_services=[])
    yield r
    await r.close()


@pytest.fixture
def container(repo):
    return AppContainer(
        config=object(), repository=repo, drivers=object(), sync=object(),
        optimizer=object(),
    )


def make_app(container):
    app = FastAPI()
    install(app, container)

    @app.get("/probe")
    async def probe(r=Depends(get_repo)):
        return {"id": id(r)}

    @app.get("/all")
    async def all_providers(c=Depends(get_container), s=Depends(get_sync), o=Depends(get_optimizer),
                            cfg=Depends(get_config), d=Depends(get_drivers)):
        return {
            "container": c is container, "sync": s is container.sync,
            "optimizer": o is container.optimizer, "config": cfg is container.config,
            "drivers": d is container.drivers,
        }

    return app


def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_install_makes_get_repo_return_container_repo(container):
    app = make_app(container)
    assert app.state.container is container
    async with client(app) as c:
        resp = await c.get("/probe")
    assert resp.json() == {"id": id(container.repository)}


async def test_dependency_override_substitutes_fake(container):
    app = make_app(container)
    fake = object()
    app.dependency_overrides[get_repo] = lambda: fake
    async with client(app) as c:
        resp = await c.get("/probe")
    assert resp.json() == {"id": id(fake)}


async def test_other_providers_match_container(container):
    app = make_app(container)
    async with client(app) as c:
        resp = await c.get("/all")
    assert resp.json() == {k: True for k in ("container", "sync", "optimizer", "config", "drivers")}


async def test_every_provider_returns_the_installed_field_through_a_request(container):
    app = make_app(container)
    assert app.state.container is container
    async with client(app) as c:
        resp = await c.get("/all")
    assert resp.status_code == 200
    assert resp.json() == {k: True for k in ("container", "sync", "optimizer", "config", "drivers")}


async def test_request_without_installed_container_fails(container):
    app = FastAPI()  # no install(): no app.state.container

    @app.get("/probe")
    async def probe(r=Depends(get_repo)):
        return {"id": id(r)}

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                           base_url="http://test") as c:
        resp = await c.get("/probe")
    assert resp.status_code == 500


async def test_installing_replaces_the_previous_container(container, repo):
    app = make_app(container)
    other = AppContainer(config=None, repository=repo, drivers=None, sync=None, optimizer=None)
    install(app, other)
    assert app.state.container is other
