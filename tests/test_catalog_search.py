"""Finding a title in the add dialog: SIMKL's search is asked for a full page per kind, and the best match (an exact
title) comes first, whichever catalogue answered. The US "Saturday Night Live" is the 11th SIMKL answer."""

import asyncio
import time

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from penguwatch.drivers.base import Title
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL
from penguwatch.library import parse_title_ref
from penguwatch.main import app
from penguwatch.textsearch import rank_matches, title_names
from tests.test_simkl_politeness import env, no_spacing  # noqa: F401  (fixtures)

SNL = "Saturday Night Live"


def t(title, year=None, **kw):
    return Title(media_type="show", title=title, year=year, **kw)


def order(query, *titles):
    return [x.title for x in rank_matches(query, titles, names=title_names)]


# --- the ranking (pure) ------------------------------------------------------------------------------------------

def test_exact_match_beats_starts_with_beats_the_rest():
    assert order("snl", t("Digital Shorts"), t("SNL50"), t("SNL"), t("Weekend Update")) == [
        "SNL", "SNL50", "Digital Shorts", "Weekend Update"]


def test_case_accents_and_punctuation_are_ignored():
    assert order("Pokemon", t("Pokemon Concierge"), t("Pokémon")) == ["Pokémon", "Pokemon Concierge"]
    assert order("rezero", t("Re:Zero kara"), t("Other"), t("Re:Zero")) == ["Re:Zero", "Re:Zero kara", "Other"]
    assert order("  SATURDAY night live!", t("X"), t(SNL)) == [SNL, "X"]


def test_english_and_alternative_names_count():
    original = t("Kusuriya no Hitorigoto", en_title="The Apothecary Diaries")
    by_alias = t("Other", alt_titles=[{"name": "Apothecary Diaries"}, "Diaries"])
    assert order("the apothecary diaries", t("Nothing"), original) == ["Kusuriya no Hitorigoto", "Nothing"]
    assert order("apothecary diaries", t("Nothing"), by_alias) == ["Other", "Nothing"]


def test_ties_keep_the_original_order_and_exact_matches_go_by_year():
    assert order("zed", t("Zed B"), t("Zed A"), t("Other")) == ["Zed B", "Zed A", "Other"]
    exact = [t(SNL, 2020), t(SNL, 1975), t(SNL, None), t(SNL, 1975)]
    ranked = rank_matches(SNL, exact, names=title_names)
    assert [x.year for x in ranked] == [1975, 1975, 2020, None]
    assert ranked[0] is exact[1] and ranked[1] is exact[3]  # equal years: the catalogue's order
    assert rank_matches("", exact) == exact  # nothing to rank by


# --- pasted links and ids ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, ref", [
    ("https://www.themoviedb.org/tv/1667-saturday-night-live", ("tmdb", 1667, "show")),
    ("https://www.themoviedb.org/movie/603", ("tmdb", 603, "movie")),
    ("https://www.imdb.com/title/tt0072562/?ref_=x", ("imdb", "tt0072562", None)),
    ("TT0072562", ("imdb", "tt0072562", None)),
    ("1923", None), ("tt1", None), ("Saturday Night Live", None),
])
def test_title_links_and_ids(text, ref):
    assert parse_title_ref(text) == ref


@pytest.mark.parametrize("text, ref", [
    ("https://simkl.com/tv/5767/saturday-night-live", ("simkl", 5767, "show")),
    ("simkl.com/anime/38/x", ("simkl", 38, "anime")),
    ("https://simkl.com/movies/12/y", ("simkl", 12, "movie")),
    ("https://www.themoviedb.org/tv/1", None), ("Saturday Night Live", None),
])
def test_simkl_links(text, ref):
    from penguwatch.drivers.simkl.service import SimklCatalog
    assert SimklCatalog.parse_link(text) == ref


# --- SIMKL: a full page per kind, best match first ------------------------------------------------------------------

def thirty_tv(position=11):
    """SIMKL's 30 TV answers for "Saturday Night Live": the US show is at `position`, after spinoffs and remakes."""
    items = [{"title": f"SNL Spinoff {i}", "year": 2000 + i % 20, "ids": {"simkl": 100 + i}} for i in range(30)]
    items[position - 1] = {"title": SNL, "year": 1975, "ids": {"simkl": 5767}}
    items[2] = {"title": SNL, "year": 2013, "ids": {"simkl": 900}}  # a remake from another country, ranked earlier
    return items


def fake_simkl(per_request_delay=0.0, tv=None):
    """Routes for a SIMKL that honours `limit` (default 10) and takes `per_request_delay` seconds per request."""
    calls = []

    async def search(request, kind):
        calls.append(request.url.path)
        await asyncio.sleep(per_request_delay)
        limit = int(request.url.params.get("limit", 10))
        return httpx.Response(200, json=(tv or thirty_tv())[:limit] if kind == "tv" else [])

    async def details(request):
        calls.append(request.url.path)
        await asyncio.sleep(per_request_delay)
        return httpx.Response(200, json={"network": "NBC"})

    respx.get(f"{SIMKL_BASE_URL}/search/tv").mock(side_effect=lambda r: search(r, "tv"))
    respx.get(f"{SIMKL_BASE_URL}/search/anime").mock(side_effect=lambda r: search(r, "anime"))
    respx.get(f"{SIMKL_BASE_URL}/search/movie").mock(side_effect=lambda r: search(r, "movie"))
    respx.get(url__regex=rf"{SIMKL_BASE_URL}/tv/\d+").mock(side_effect=details)
    return calls


@respx.mock
async def test_the_exact_match_at_11_is_found_and_listed_first(env):
    _, client, sync = env
    fake_simkl()
    found = await sync.service.catalog.search(SNL, ("show", "anime", "movie"))
    assert [(x.ids["simkl"], x.year) for x in found[:2]] == [(5767, 1975), (900, 2013)]
    assert len(found) == 30
    # the best matches are the ones that come back with their details
    assert found[0].network == "NBC"


@respx.mock
async def test_a_search_costs_the_same_requests_with_a_bigger_page(env):
    _, client, sync = env
    calls = fake_simkl()
    await sync.service.catalog.search(SNL, ("show", "anime", "movie"))
    assert sorted(calls)[:3] == ["/search/anime", "/search/movie", "/search/tv"]
    assert len(calls) == 3 + 8  # one request per kind, details for the first eight only
    assert [dict(c.request.url.params)["limit"] for c in respx.calls if c.request.url.path == "/search/tv"] == ["30"]


@respx.mock
async def test_latency_with_the_bigger_page_is_unchanged(env, capsys):
    _, client, sync = env
    fake_simkl(per_request_delay=0.15)
    timings = {}
    for limit in (10, 30):
        start = time.monotonic()
        await sync.service.catalog.search(SNL, ("show", "anime", "movie"), limit=limit)
        timings[limit] = time.monotonic() - start
    with capsys.disabled():
        print(f"\nsearch latency, 150 ms per request: limit 10 {timings[10]:.2f}s, limit 30 {timings[30]:.2f}s")
    assert timings[30] < timings[10] + 0.1  # same number of round trips


# --- the route ------------------------------------------------------------------------------------------------------

@respx.mock
async def test_the_route_lists_the_exact_match_first(container):
    container.drivers.get("simkl").client.client_id = "cid"
    fake_simkl()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = (await ac.get("/api/catalog/search", params={"q": SNL, "type": "tv"})).json()
    assert [r["ids"]["simkl"] for r in res[:2]] == [5767, 900] and len(res) == 30


async def test_the_route_ranks_whatever_order_a_catalogue_answers_in(container, monkeypatch):
    async def search(q, media_types, **kw):
        return [t("SNL50", ids={"simkl": 1}), t("SNL", 1975, ids={"simkl": 2})]

    catalog = container.drivers.catalogs[0]
    container.drivers.get("simkl").client.client_id = "cid"
    monkeypatch.setattr(catalog, "search", search)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = (await ac.get("/api/catalog/search", params={"q": "snl"})).json()
    assert [r["title"] for r in res] == ["SNL", "SNL50"]


@respx.mock
async def test_a_pasted_simkl_link_offers_that_one_title(container):
    container.drivers.get("simkl").client.client_id = "cid"
    searched = respx.get(url__regex=rf"{SIMKL_BASE_URL}/search/.*")
    respx.get(f"{SIMKL_BASE_URL}/tv/5767").respond(json={"title": SNL, "year": 1975, "ids": {"simkl": 5767}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = (await ac.get("/api/catalog/search", params={"q": "https://simkl.com/tv/5767/saturday-night-live"})).json()
    assert [(r["title"], r["ids"]["simkl"], r["media_type"]) for r in res] == [(SNL, 5767, "show")]
    assert not searched.called


@respx.mock
async def test_a_pasted_imdb_or_tmdb_link_is_resolved_through_the_catalogue(container):
    container.drivers.get("simkl").client.client_id = "cid"
    lookups = respx.get(f"{SIMKL_BASE_URL}/search/id").respond(json=[{"ids": {"simkl": 5767}}])
    respx.get(f"{SIMKL_BASE_URL}/tv/5767").respond(json={"title": SNL, "year": 1975, "ids": {"simkl": 5767}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        by_tmdb = (await ac.get("/api/catalog/search", params={"q": "https://www.themoviedb.org/tv/1667-snl"})).json()
        by_imdb = (await ac.get("/api/catalog/search", params={"q": "tt0072562"})).json()
    assert [r["ids"]["simkl"] for r in by_tmdb] == [5767] and [r["ids"]["simkl"] for r in by_imdb] == [5767]
    params = [dict(c.request.url.params) for c in lookups.calls]
    assert params[0]["tmdb"] == "1667" and params[0]["type"] == "show" and params[1]["imdb"] == "tt0072562"


@respx.mock
async def test_an_unknown_link_finds_nothing_instead_of_searching(container):
    container.drivers.get("simkl").client.client_id = "cid"
    respx.get(f"{SIMKL_BASE_URL}/search/id").respond(json=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/catalog/search", params={"q": "tt9999999"})).json() == []


def test_a_driver_says_it_takes_links_when_its_catalogue_parses_them():
    """/api/drivers `takes_links` (Add title's hint names the service) agrees with the catalogue's `parse_link`."""
    from penguwatch.drivers import DRIVERS
    from penguwatch.drivers.simkl.service import SimklCatalog
    from penguwatch.drivers.tvmaze.service import TvmazeCatalog

    catalogues = {"simkl": SimklCatalog, "tvmaze": TvmazeCatalog}
    for name, catalogue in catalogues.items():
        assert bool(getattr(DRIVERS[name], "takes_links", False)) == callable(getattr(catalogue, "parse_link", None)), name
    assert DRIVERS["simkl"].takes_links is True

