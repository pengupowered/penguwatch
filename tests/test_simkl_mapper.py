"""Characterization tests for the SIMKL response mapper (parse of library/catalogue items)."""

from penguwatch.drivers.simkl import mapper
import pytest

from penguwatch.models import HouseholdTag, MediaType, Priority, ShowStatus
from tests.simkl_helpers import simkl_engine

SHOW_ITEM = {
    "status": "watching",
    "user_rating": 8,
    "last_watched_at": "2026-01-02T03:04:05Z",
    "last_watched": "S01E03",
    "watched_episodes_count": 3,
    "total_episodes_count": 10,
    "not_aired_episodes_count": 2,
    "next_to_watch_info": {"season": 1, "episode": 4},
    "next_to_watch": "S09E09",
    "show": {
        "title": "Test Show",
        "year": 2020,
        "poster": "abc123",
        "fanart": "fan456",
        "overview": "An overview",
        "network": "HBO",
        "runtime": 50,
        "status": "Ended",
        "genres": ["Drama", "Crime"],
        "ids": {"simkl": 111, "imdb": "tt111", "tmdb": 222, "tvdb": 333},
        "ratings": {"simkl": {"rating": 8.1}, "imdb": {"rating": 8.5, "votes": 1200}},
    },
}

ANIME_ITEM = {
    "status": "completed",
    "watched_episodes_count": 12,
    "total_episodes_count": 12,
    "last_watched": "E12",
    "anime": {
        "title": "Test Anime",
        "year": "2019",
        "poster": "http://example.com/p.jpg",
        "genres": ["Action"],
        "ids": {"simkl": 5, "mal": 99},
        "ratings": {"simkl": {"rating": 7.0}, "mal": {"rating": 8.2}},
    },
}

ANIME_NO_MAL = {
    "status": "plantowatch",
    "anime": {
        "title": "Anime Two",
        "genres": ["Anime", "Comedy"],
        "ids": {"simkl": 6},
        "ratings": {"simkl": {"rating": 6.5}},
    },
}

MOVIE_DONE = {
    "status": "completed",
    "user_rating": 9,
    "movie": {
        "title": "Test Movie",
        "year": 2010,
        "runtime": "120",
        "status": "released",
        "ids": {"simkl": 77, "imdb": "tt77", "tmdb": "1234"},
        "ratings": {"imdb": {"rating": 7.7, "votes": 5}},
    },
}

MOVIE_PLAN = {"status": "plantowatch", "movie": {"title": "Later Movie", "ids": {"simkl": 78}}}

FLAT_ITEM = {
    "title": "Flat Show",
    "year": "not-a-year",
    "genres": "Drama, , Comedy ,",
    "aired_episodes": 7,
    "total_episodes": 9,
    "runtime": "",
    "status": "ENDED",
    "ids": {"simkl_id": 42, "tmdb": "555", "tvdb": "x"},
    "poster": "pp",
}

EXPECTED_SHOW = {'id': None,
 'ids': {'simkl': 111, 'imdb': 'tt111', 'tmdb': 222, 'tvdb': 333},
 'media_type': MediaType.SHOW,
 'title': 'Test Show',
 'year': 2020,
 'poster': 'https://simkl.in/posters/abc123_m.jpg',
 'banner': 'https://simkl.in/fanart/fan456_medium.jpg',
 'overview': 'An overview',
 'genres': ['Drama', 'Crime'],
 'network': 'HBO',
 'service_id': None,
 'service_manual': False,
 'status': ShowStatus.WATCHING,
 'priority': Priority.MEDIUM,
 'household_tag': HouseholdTag.PERSON1,
 'simkl_rating': 8.1,
 'imdb_rating': 8.5,
 'imdb_votes': 1200,
 'user_rating': 8.0,
 'total_episodes': 10,
 'watched_episodes': 3,
 'aired_episodes': 8,
 'runtime_minutes': 50,
 'next_episode_air_date': None,
 'next_episode_season': None,
 'next_episode_number': None,
 'next_episode_title': None,
 'is_ended': True,
 'last_watched': 'S01E03',
 'next_to_watch': 'S01E04',
 'notes': None,
 'last_watched_at': '2026-01-02T03:04:05Z',
 'episodes_synced_at': None,
 'enriched_at': None,
 'sequels': None,
 'created_at': None,
 'updated_at': None,
 'episodes_left': 5,
 'is_caught_up': False,
 'is_ready_to_binge': True}


EXPECTED_ANIME = {'id': None,
 'ids': {'simkl': 5},
 'media_type': MediaType.ANIME,
 'title': 'Test Anime',
 'year': 2019,
 'poster': 'http://example.com/p.jpg',
 'banner': None,
 'overview': None,
 'genres': ['Action', 'Anime'],
 'network': None,
 'service_id': None,
 'service_manual': False,
 'status': ShowStatus.COMPLETED,
 'priority': Priority.MEDIUM,
 'household_tag': HouseholdTag.PERSON1,
 'simkl_rating': 7.0,
 'imdb_rating': 8.2,
 'imdb_votes': None,
 'user_rating': None,
 'total_episodes': 12,
 'watched_episodes': 12,
 'aired_episodes': 12,
 'runtime_minutes': 24,
 'next_episode_air_date': None,
 'next_episode_season': None,
 'next_episode_number': None,
 'next_episode_title': None,
 'is_ended': False,
 'last_watched': 'E12',
 'next_to_watch': None,
 'notes': None,
 'last_watched_at': None,
 'episodes_synced_at': None,
 'enriched_at': None,
 'sequels': None,
 'created_at': None,
 'updated_at': None,
 'episodes_left': 0,
 'is_caught_up': True,
 'is_ready_to_binge': False}


EXPECTED_MOVIE = {'id': None,
 'ids': {'simkl': 77, 'imdb': 'tt77', 'tmdb': 1234},
 'media_type': MediaType.MOVIE,
 'title': 'Test Movie',
 'year': 2010,
 'poster': None,
 'banner': None,
 'overview': None,
 'genres': [],
 'network': None,
 'service_id': None,
 'service_manual': False,
 'status': ShowStatus.COMPLETED,
 'priority': Priority.MEDIUM,
 'household_tag': HouseholdTag.PERSON1,
 'simkl_rating': None,
 'imdb_rating': 7.7,
 'imdb_votes': 5,
 'user_rating': 9.0,
 'total_episodes': 1,
 'watched_episodes': 1,
 'aired_episodes': 1,
 'runtime_minutes': 120,
 'next_episode_air_date': None,
 'next_episode_season': None,
 'next_episode_number': None,
 'next_episode_title': None,
 'is_ended': True,
 'last_watched': None,
 'next_to_watch': None,
 'notes': None,
 'last_watched_at': None,
 'episodes_synced_at': None,
 'enriched_at': None,
 'sequels': None,
 'created_at': None,
 'updated_at': None,
 'episodes_left': 0,
 'is_caught_up': True,
 'is_ready_to_binge': False}


EXPECTED_FLAT = {'id': None,
 'ids': {'simkl': 42, 'tmdb': 555},
 'media_type': MediaType.SHOW,
 'title': 'Flat Show',
 'year': None,
 'poster': 'https://simkl.in/posters/pp_m.jpg',
 'banner': None,
 'overview': None,
 'genres': ['Drama', 'Comedy'],
 'network': None,
 'service_id': None,
 'service_manual': False,
 'status': ShowStatus.PLANTOWATCH,
 'priority': Priority.MEDIUM,
 'household_tag': HouseholdTag.PERSON1,
 'simkl_rating': None,
 'imdb_rating': None,
 'imdb_votes': None,
 'user_rating': None,
 'total_episodes': 9,
 'watched_episodes': 0,
 'aired_episodes': 7,
 'runtime_minutes': 45,
 'next_episode_air_date': None,
 'next_episode_season': None,
 'next_episode_number': None,
 'next_episode_title': None,
 'is_ended': True,
 'last_watched': None,
 'next_to_watch': None,
 'notes': None,
 'last_watched_at': None,
 'episodes_synced_at': None,
 'enriched_at': None,
 'sequels': None,
 'created_at': None,
 'updated_at': None,
 'episodes_left': 7,
 'is_caught_up': False,
 'is_ready_to_binge': True}


@pytest.fixture
def svc():
    return simkl_engine(None, None)


def test_library_show_item(svc):
    show = mapper.parse_show_item(SHOW_ITEM)
    assert show.model_dump() == EXPECTED_SHOW


def test_anime_item_mal_rating(svc):
    show = mapper.parse_show_item(ANIME_ITEM)
    assert show.model_dump() == EXPECTED_ANIME


def test_anime_item_simkl_rating_fallback_and_no_duplicate_genre(svc):
    show = mapper.parse_show_item(ANIME_NO_MAL)
    assert show.media_type == MediaType.ANIME
    assert show.genres == ["Anime", "Comedy"]
    assert show.imdb_rating == 6.5 and show.simkl_rating == 6.5
    assert show.runtime_minutes == 24
    assert show.status.value == "plantowatch"
    assert show.is_ended is False


def test_is_anime_flag_on_flat_item(svc):
    show = mapper.parse_show_item({"title": "X", "ids": {"simkl": 1}}, is_anime=True)
    assert show.media_type == MediaType.ANIME
    assert show.genres == ["Anime"]
    assert show.runtime_minutes == 24


def test_explicit_media_type_wins(svc):
    show = mapper.parse_show_item(ANIME_ITEM, media_type=MediaType.SHOW)
    assert show.media_type == MediaType.SHOW
    assert show.genres == ["Action"]
    assert show.runtime_minutes == 45
    assert show.imdb_rating is None


def test_movie_completed(svc):
    show = mapper.parse_show_item(MOVIE_DONE)
    assert show.model_dump() == EXPECTED_MOVIE


def test_movie_not_completed(svc):
    show = mapper.parse_show_item(MOVIE_PLAN)
    assert show.media_type == MediaType.MOVIE
    assert (show.total_episodes, show.aired_episodes, show.watched_episodes) == (1, 1, 0)
    assert show.runtime_minutes == 110
    assert show.is_ended is True
    assert show.next_to_watch is None and show.last_watched is None


def test_flat_catalogue_item(svc):
    show = mapper.parse_show_item(FLAT_ITEM)
    assert show.model_dump() == EXPECTED_FLAT


@pytest.mark.parametrize("item", [{}, {"status": "watching"}, {"show": {}}, {"show": {"title": ""}}, {"movie": {"ids": {}}}])
def test_items_without_title_are_none(svc, item):
    assert mapper.parse_show_item(item) is None


def test_next_to_watch_info_partial_falls_back(svc):
    item = {"show": {"title": "T", "ids": {"simkl": 1}}, "next_to_watch_info": {"season": 1}, "next_to_watch": "S02E02"}
    assert mapper.parse_show_item(item).next_to_watch == "S02E02"


def test_not_aired_clamped_at_zero(svc):
    item = {"show": {"title": "T", "ids": {"simkl": 1}}, "total_episodes_count": 2, "not_aired_episodes_count": 5}
    show = mapper.parse_show_item(item)
    assert show.aired_episodes == 0 and show.total_episodes == 2


def test_aired_defaults_to_total(svc):
    item = {"show": {"title": "T", "episodes_count": 8, "ids": {"simkl": 1}}}
    show = mapper.parse_show_item(item)
    assert show.total_episodes == 8 and show.aired_episodes == 8


def test_ratings_not_dict_ignored(svc):
    item = {"show": {"title": "T", "ids": {"simkl": 1}, "ratings": "n/a"}}
    show = mapper.parse_show_item(item)
    assert show.simkl_rating is None and show.imdb_rating is None


def test_extract_watched():
    f = mapper.extract_watched
    assert f({}) is None
    assert f({"seasons": "x"}) is None
    assert f({"seasons": []}) == []
    item = {"seasons": [
        {"number": 1, "episodes": [{"number": 1, "watched_at": "a"}, {"number": 2}, {"watched_at": "z"}]},
        {"episodes": [{"number": 5}]},
        {"number": "2", "episodes": [{"number": "3", "watched_at": "b"}]},
        {"number": 3},
    ]}
    assert f(item) == [(1, 1, "a"), (1, 2, None), (2, 3, "b")]


def test_item_obj():
    f = mapper.item_obj
    assert f({"show": {"a": 1}, "movie": {"b": 2}}) == {"a": 1}
    assert f({"movie": {"b": 2}}) == {"b": 2}
    assert f({"anime": {"c": 3}}) == {"c": 3}
    flat = {"title": "x"}
    assert f(flat) is flat
    assert f({"show": {}, "title": "y"}) == {"show": {}, "title": "y"}


def test_parse_episode_code():
    from penguwatch.db.episodes import parse_episode_code
    assert parse_episode_code(None) is None
    assert parse_episode_code("") is None
    assert parse_episode_code("S01E02") == (1, 2)
    assert parse_episode_code("  s3 e10 extra") == (3, 10)
    assert parse_episode_code("E12") == (1, 12)
    assert parse_episode_code("12") is None
    assert parse_episode_code("Season 1") is None


def test_describe_helpers():
    from penguwatch.drivers.base import SyncOp
    from penguwatch.sync.ops import describe, describe_episodes
    assert describe_episodes([]) == "no episodes"
    assert describe_episodes([(1, 2)]) == "S01E02"
    assert describe_episodes([(1, 3), (1, 2), (1, 2)]) == "S01E02, S01E03"
    assert describe_episodes([(1, 1), (1, 2), (1, 3), (1, 4)]) == "S01E01 – S01E04 (4 episodes)"

    def op(kind, media="show", **kw):
        return SyncOp(kind=kind, media_type=media, ids={"simkl": 1}, **kw)

    assert describe(op("status", status="completed")) == "Set status to Completed"
    assert describe(op("watched", "movie")) == "Mark movie watched"
    assert describe(op("unwatched")) == "Mark whole title unwatched"
    assert describe(op("watched", episodes=[(1, 2)])) == "Mark S01E02 watched"
    assert describe(op("rate", rating=7)) == "Rate 7/10"
    assert describe(op("unrate")) == "Remove rating"
    assert describe(op("remove"), "SIMKL") == "Remove from SIMKL (lists and watch history)"
    assert describe(op("weird")) == "weird"


def test_mapper_functions_match_service_methods(svc):
    from penguwatch.drivers.simkl import mapper
    for item in (SHOW_ITEM, ANIME_ITEM, ANIME_NO_MAL, MOVIE_DONE, MOVIE_PLAN, FLAT_ITEM):
        assert mapper.parse_show_item(item) == mapper.parse_show_item(item)
    assert mapper.parse_show_item({"title": "X"}, is_anime=True).media_type == MediaType.ANIME
    assert mapper.parse_show_item({"title": "X"}, media_type=MediaType.MOVIE).media_type == MediaType.MOVIE
    assert mapper.parse_show_item({}) is None
    assert mapper.item_obj({"anime": {"c": 3}}) == {"c": 3}
    assert mapper.extract_watched({"seasons": [{"number": 1, "episodes": [{"number": 2}]}]}) == [(1, 2, None)]


def test_mapper_has_no_io_or_repository_dependencies():
    import ast
    import inspect
    from penguwatch.drivers.simkl import mapper
    tree = ast.parse(inspect.getsource(mapper))
    assert not any(isinstance(n, (ast.Await, ast.AsyncFunctionDef)) for n in ast.walk(tree))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "penguwatch.db.repository" not in modules
    assert not any(isinstance(n, ast.Name) and n.id == "self" for n in ast.walk(tree))

