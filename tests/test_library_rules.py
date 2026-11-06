"""The title-creation rules, tested without a database, a network or global state."""

import pytest

from penguwatch import library
from penguwatch.drivers.simkl.mapper import details_title, search_title
from penguwatch.models import MediaType, ShowCreateRequest, ShowStatus


def req(**kw):
    return ShowCreateRequest(title=kw.pop("title", "Example"), **kw)


def details(record, media_type=MediaType.SHOW):
    """A catalogue's details as the rules get them: here SIMKL's record through the SIMKL driver's mapping."""
    return details_title(record, media_type)


def ratings(r, media_type):
    return details({"title": "x", "ratings": r}, media_type)


def test_search_media_types_by_requested_type():
    assert library.search_media_types(MediaType.MOVIE) == ("movie",)
    assert library.search_media_types(MediaType.SHOW) == ("show", "anime")
    assert library.search_media_types(None) == ("show", "anime", "movie")


def test_pick_search_match_prefers_the_requested_year():
    matches = [{"id": 1, "year": 1999}, {"id": 2, "year": 2021}, {"id": 3, "year": None}]
    assert library.pick_search_match(matches, 2021)["id"] == 2
    assert library.pick_search_match(matches, 2020)["id"] == 2  # within a year
    assert library.pick_search_match(matches, 1700)["id"] == 3  # nothing near: first with unknown year
    assert library.pick_search_match(matches, None)["id"] == 1
    assert library.pick_search_match([], 2021) is None


def test_anime_without_imdb_falls_back_to_mal_then_simkl():
    r = {"mal": {"rating": 8.1}, "simkl": {"rating": 7.5}}
    assert library.extract_ratings(ratings(r, MediaType.ANIME), MediaType.ANIME, None) == (8.1, 7.5, None)
    assert library.extract_ratings(ratings({"simkl": {"rating": 7.5}}, MediaType.ANIME), MediaType.ANIME, None) == (
        7.5, 7.5, None)
    # shows never borrow MAL/SIMKL for the IMDb field
    assert library.extract_ratings(ratings(r, MediaType.SHOW), MediaType.SHOW, None) == (None, 7.5, None)
    assert library.extract_ratings(ratings({"imdb": {"rating": 6.0, "votes": 10}}, MediaType.ANIME), MediaType.ANIME,
                                   None) == (6.0, None, 10)
    assert library.extract_ratings(ratings("garbage", MediaType.SHOW), MediaType.SHOW, 5.0) == (5.0, None, None)


def test_merge_prefers_catalogue_values_and_unions_genres():
    p = req(title="Typed Name", year=2020, genres=["Drama"], imdb_rating=6.0, network="Typed Network")
    d = {"title": "Catalogue Name", "year": 2021, "genres": "Drama, Mystery", "network": "Apple TV+",
         "runtime": "52", "status": "Ended", "total_episodes": 10,
         "ids": {"imdb": "tt1", "tmdb": "99"}, "ratings": {"imdb": {"rating": 8.2, "votes": 5000}}}
    m = library.merge_details(p, details(d), MediaType.SHOW)
    assert (m.title, m.year, m.network) == ("Catalogue Name", 2021, "Apple TV+")
    assert m.genres == ["Drama", "Mystery"]
    assert m.runtime == 52 and m.is_ended and m.total_episodes == m.aired_episodes == 10
    assert (m.ids.get("imdb"), m.ids.get("tmdb"), m.imdb_rating, m.imdb_votes) == ("tt1", 99, 8.2, 5000)
    assert m.enriched is True


def test_merge_without_details_keeps_the_request():
    m = library.merge_details(req(title="Manual", year=2001, network="Net"), None, MediaType.SHOW)
    assert (m.title, m.year, m.network, m.enriched) == ("Manual", 2001, "Net", False)


def test_bad_catalogue_numbers_are_ignored():
    m = library.merge_details(req(), details({"runtime": "n/a", "ids": {"tmdb": "oops"}}), MediaType.SHOW)
    assert m.runtime is None and m.ids.get("tmdb") is None


@pytest.mark.parametrize("given,expected_status,expected_watched", [
    (ShowStatus.WATCHING, ShowStatus.PLANTOWATCH, 0),
    (ShowStatus.HOLD, ShowStatus.PLANTOWATCH, 0),
    (ShowStatus.COMPLETED, ShowStatus.COMPLETED, 1),
    (ShowStatus.PLANTOWATCH, ShowStatus.PLANTOWATCH, 0),
])
def test_movie_status_rules(given, expected_status, expected_watched):
    assert library.normalize_status(MediaType.MOVIE, given, 5) == (expected_status, expected_watched)


def test_shows_keep_their_status_and_progress():
    assert library.normalize_status(MediaType.SHOW, ShowStatus.WATCHING, 4) == (ShowStatus.WATCHING, 4)


def test_assemble_applies_media_type_defaults():
    anime = library.assemble_show(req(), library.merge_details(req(), None, MediaType.ANIME),
                                  ids={"simkl": 1}, media_type=MediaType.ANIME, service_id=None, network=None)
    assert "Anime" in anime.genres and anime.runtime_minutes == 24

    movie_req = req(status=ShowStatus.WATCHING)
    movie = library.assemble_show(movie_req, library.merge_details(movie_req, None, MediaType.MOVIE),
                                  ids={"simkl": 2}, media_type=MediaType.MOVIE, service_id="netflix", network="Netflix")
    assert movie.total_episodes == movie.aired_episodes == 1 and movie.is_ended
    assert movie.status == ShowStatus.PLANTOWATCH and movie.runtime_minutes == 110
    assert movie.service_id == "netflix"
    assert movie.service_manual is False  # service came from matching, not from the request


def test_new_titles_get_the_english_title_and_alternative_names():
    d = {"title": "Kusuriya no Hitorigoto", "en_title": "The Apothecary Diaries",
         "alt_titles": [{"name": "薬屋のひとりごと"}, "Drugstore Soliloquy"]}
    m = library.merge_details(req(), details(d, MediaType.ANIME), MediaType.ANIME)
    assert (m.en_title, m.alt_titles) == ("The Apothecary Diaries", ["薬屋のひとりごと", "Drugstore Soliloquy"])
    show = library.assemble_show(req(), m, ids={"simkl": 1}, media_type=MediaType.ANIME, service_id=None, network=None)
    assert show.en_title == "The Apothecary Diaries" and show.alt_titles == ["薬屋のひとりごと", "Drugstore Soliloquy"]
    # a search record names it title_en; an English title that restates the title is dropped
    record = search_title({"title": "Frieren", "title_en": "Frieren: Beyond Journey&#039;s End", "media_type": "anime"})
    assert library.merge_details(req(), record, MediaType.ANIME).en_title == "Frieren: Beyond Journey\x27s End"
    assert library.merge_details(req(), details({"title": "Shy", "en_title": "SHY"}, MediaType.ANIME),
                                 MediaType.ANIME).en_title is None
