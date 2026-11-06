"""Pins what enrichment persists from SIMKL details, branch by branch.

The same CASES table drives two tests: one through `enrich_titles` (the fields
handed to `update_show_fields`) and one directly against the pure `merge_details`.
"""

import json

import pytest

from penguwatch.config import ServiceConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show, MediaType
from penguwatch.drivers.simkl.client import SimklClient, poster_url, fanart_url
from tests.simkl_helpers import simkl_engine

POSTER = poster_url("12/abcdef")
BANNER = fanart_url("34/fedcba")

ANIME = {"media_type": MediaType.ANIME}
MOVIE = {"media_type": MediaType.MOVIE}
REL = {"relation_type": "sequel", "is_direct": True, "anime_type": "tv", "title": "S2", "en_title": "Season 2",
       "year": 2025, "poster": "99/zz", "ids": {"simkl": 777}}

# id -> (show kwargs, details, expected fields beyond enriched_at/service_id)
CASES = {
    "network": ({}, {"network": "HBO"}, {"network": "HBO"}),
    "overview_when_missing": ({}, {"overview": "Plot"}, {"overview": "Plot"}),
    "overview_kept_when_present": ({"overview": "Mine"}, {"overview": "Plot"}, {}),
    "banner_when_missing": ({}, {"fanart": "34/fedcba"}, {"banner": BANNER}),
    "banner_kept_when_present": ({"banner": "x"}, {"fanart": "34/fedcba"}, {}),
    "poster_when_missing": ({}, {"poster": "12/abcdef"}, {"poster": POSTER}),
    "poster_kept_when_present": ({"poster": "x"}, {"poster": "12/abcdef"}, {}),
    "runtime": ({}, {"runtime": "52"}, {"runtime_minutes": 52}),
    "runtime_invalid_ignored": ({}, {"runtime": "n/a"}, {}),
    "runtime_zero_ignored": ({}, {"runtime": 0}, {}),
    "status_ended": ({}, {"status": "Ended"}, {"is_ended": 1}),
    "status_running": ({}, {"status": "returning"}, {"is_ended": 0}),
    "status_ignored_for_movie": (MOVIE, {"status": "ended"}, {}),
    "total_episodes_when_zero": ({}, {"total_episodes": 12}, {"total_episodes": 12, "aired_episodes": 12}),
    "total_episodes_kept_when_known": ({"total_episodes": 8}, {"total_episodes": 12}, {}),
    "genres_list_merged": ({"genres": ["Drama"]}, {"genres": ["Sci-Fi", "Drama"]},
                           {"genres_json": json.dumps(["Drama", "Sci-Fi"])}),
    "genres_string_keeps_empty_pieces": ({"genres": ["Drama"]}, {"genres": "Sci-Fi, Mystery, "},
                                         {"genres_json": json.dumps(["Drama", "Sci-Fi", "Mystery", ""])}),
    "genres_empty_ignored": ({}, {"genres": ""}, {}),
    "ids_fill_missing": ({}, {"ids": {"imdb": "tt1", "tmdb": 2, "tvdb": 3}},
                         {"ids": {"imdb": "tt1", "tmdb": 2, "tvdb": 3}}),
    "ids_not_overwritten": ({"ids": {"imdb": "tt0", "tmdb": 20}}, {"ids": {"imdb": "tt1", "tmdb": 2, "tvdb": 3}},
                            {"ids": {"tvdb": 3}}),
    "imdb_rating_and_votes": ({}, {"ratings": {"imdb": {"rating": 8.4, "votes": 1200}}},
                              {"imdb_rating": 8.4, "imdb_votes": 1200}),
    "simkl_rating": ({}, {"ratings": {"simkl": {"rating": 7.9}}}, {"simkl_rating": 7.9}),
    "mal_fallback_for_anime": (ANIME, {"ratings": {"mal": {"rating": 8.1}}}, {"imdb_rating": 8.1, "sequels_json": "[]"}),
    "mal_skipped_when_imdb_present": (
        ANIME, {"ratings": {"imdb": {"rating": 7.0, "votes": 5}, "mal": {"rating": 8.1}}},
        {"imdb_rating": 7.0, "imdb_votes": 5, "sequels_json": "[]"}),
    "mal_skipped_when_show_has_imdb_rating": (
        dict(ANIME, imdb_rating=6.5), {"ratings": {"mal": {"rating": 8.1}}}, {"sequels_json": "[]"}),
    "mal_ignored_for_non_anime": ({}, {"ratings": {"mal": {"rating": 8.1}}}, {}),
    "anime_sequels": (
        ANIME, {"relations": [REL]},
        {"sequels_json": json.dumps([{"ids": {"simkl": 777}, "title": "Season 2", "year": 2025,
                                      "poster": poster_url("99/zz"), "anime_type": "tv"}])}),
    "details_missing": ({}, {}, {}),
    "details_missing_anime": (ANIME, {}, {}),
}


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "enrich.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    client = SimklClient(client_id="cid", access_token="tok")
    sync = simkl_engine(client, repo)
    yield repo, client, sync
    await client.aclose()
    await repo.close()


async def _enrich(env, kwargs, details, monkeypatch=None):
    """Runs enrich_titles for one stored show; returns the fields handed to update_show_fields."""
    repo, client, sync = env
    kwargs = {**kwargs, "ids": {"simkl": 4242, **kwargs.get("ids", {})}}
    sid = await repo.upsert_show(Show(title="T", service_manual=True, **kwargs))
    calls = []
    real = repo.update_show_fields

    async def spy(show_id, fields):
        calls.append((show_id, dict(fields)))
        await real(show_id, fields)

    async def get_details(media, simkl_id):
        if isinstance(details, Exception):
            raise details
        return details

    repo.update_show_fields = spy
    client.get_details = get_details
    assert await sync.enrich_titles([sid]) == 1
    assert len(calls) == 1 and calls[0][0] == sid
    return calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CASES))
async def test_enrich_titles_persists_fields(env, name):
    kwargs, details, expected = CASES[name]
    fields = await _enrich(env, kwargs, details)
    assert fields.pop("enriched_at")
    assert fields == expected


@pytest.mark.asyncio
async def test_enrich_titles_details_error_still_stamps_enriched_at(env):
    fields = await _enrich(env, {}, RuntimeError("boom"))
    assert list(fields) == ["enriched_at"]


@pytest.mark.asyncio
async def test_enrich_titles_field_order(env):
    details = {"network": "HBO", "overview": "o", "poster": "12/abcdef", "runtime": 50, "status": "ended",
               "total_episodes": 3, "genres": ["A"], "ids": {"imdb": "tt9"},
               "ratings": {"imdb": {"rating": 8, "votes": 1}, "simkl": {"rating": 7}}}
    fields = await _enrich(env, {}, details)
    assert list(fields) == ["enriched_at", "network", "overview", "poster", "runtime_minutes", "is_ended",
                            "total_episodes", "aired_episodes", "genres_json", "ids", "imdb_rating",
                            "imdb_votes", "simkl_rating"]


@pytest.mark.asyncio
async def test_enrich_titles_matches_service_by_network(env):
    repo, client, sync = env
    sid = await repo.upsert_show(Show(title="T", ids={"simkl": 4243}))
    client.get_details = lambda media, simkl_id: _async({"network": "Apple TV+"})
    await sync.enrich_titles([sid])
    show = await repo.get_show_by_id(sid)
    assert show.network == "Apple TV+" and show.service_id == "apple_tv"


async def _async(value):
    return value


@pytest.mark.parametrize("name", list(CASES))
def test_merge_details_matches_enrichment(name):
    from penguwatch.drivers.simkl.mapper import merge_details
    kwargs, details, expected = CASES[name]
    show = Show(title="T", **{**kwargs, "ids": {"simkl": 4242, **kwargs.get("ids", {})}})
    before = show.model_dump()
    assert merge_details(show, details) == expected
    assert show.model_dump() == before  # pure: the show is not mutated


def test_merge_details_none_is_empty():
    from penguwatch.drivers.simkl.mapper import merge_details
    assert merge_details(Show(title="T"), None) == {}
