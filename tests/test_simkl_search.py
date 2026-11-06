"""SIMKL title search: the TV, anime and movie searches run concurrently."""

import asyncio

import httpx
import pytest
import respx

from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient


@pytest.fixture
async def client():
    c = SimklClient(client_id="cid", access_token="tok")
    c.retry_base_delay = 0
    yield c
    await c.aclose()


def hit(sid, title="T"):
    return {"title": title, "ids": {"simkl": sid}}


@pytest.mark.asyncio
@respx.mock
async def test_searches_run_concurrently_and_results_keep_kind_order(client):
    movie_started = asyncio.Event()

    async def tv(request):
        await movie_started.wait()  # only returns once the movie search is in flight
        return httpx.Response(200, json=[hit(1)])

    async def movie(request):
        movie_started.set()
        return httpx.Response(200, json=[hit(3)])

    respx.get(f"{SIMKL_BASE_URL}/search/tv").mock(side_effect=tv)
    respx.get(f"{SIMKL_BASE_URL}/search/anime").respond(json=[hit(2)])
    respx.get(f"{SIMKL_BASE_URL}/search/movie").mock(side_effect=movie)

    results = await asyncio.wait_for(client.search("x", enrich=0), timeout=2)  # sequential code would deadlock
    assert [(r["media_type"], r["ids"]["simkl"]) for r in results] == [("show", 1), ("anime", 2), ("movie", 3)]
    assert results[1]["is_anime"] is True


@pytest.mark.asyncio
@respx.mock
async def test_a_failing_search_does_not_hide_the_others(client):
    respx.get(f"{SIMKL_BASE_URL}/search/tv").respond(json=[hit(1)])
    respx.get(f"{SIMKL_BASE_URL}/search/anime").respond(status_code=500)
    respx.get(f"{SIMKL_BASE_URL}/search/movie").respond(json=[hit(3)])
    results = await client.search("x", enrich=0)
    assert [r["ids"]["simkl"] for r in results] == [1, 3]


@pytest.mark.asyncio
@respx.mock
async def test_dedupes_within_a_kind_but_keeps_the_same_id_across_kinds(client):
    respx.get(f"{SIMKL_BASE_URL}/search/tv").respond(json=[hit(7), hit(7), hit(8)])
    respx.get(f"{SIMKL_BASE_URL}/search/anime").respond(json=[hit(7)])
    respx.get(f"{SIMKL_BASE_URL}/search/movie").respond(json=[])
    results = await client.search("x", enrich=0)
    assert [(r["media_type"], r["ids"]["simkl"]) for r in results] == [("show", 7), ("show", 8), ("anime", 7)]


@pytest.mark.asyncio
@respx.mock
async def test_unexpected_errors_still_propagate(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(client, "_request", boom)
    with pytest.raises(RuntimeError):
        await client.search("x", enrich=0)
