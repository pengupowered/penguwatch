"""Custom rating sources: the source list, the bulk import, values on titles, sort and filter, exports, and the guards
(token, origin, limits, scale, slug)."""

import csv
import io
import sqlite3

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.main import app, security_config
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.ratings import RatingSourceSpec
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

SPEC = {"id": "letterboxd", "name": "Letterboxd", "label": "LB", "scale_min": 0, "scale_max": 5, "decimals": 1,
        "show_on_cards": True}


def client(**kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kw)


@pytest.fixture
def token_mode(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    return {"Authorization": "Bearer s3cret-token"}


@pytest.fixture
async def library(setup_test_app):
    repo = setup_test_app
    ids = {}
    ids["heat"] = await repo.upsert_show(Show(title="Heat", year=1995, media_type=MediaType.MOVIE, ids={"imdb": "tt0113277", "tmdb": 949, "simkl": 111}, imdb_rating=8.3, simkl_rating=8.0))
    ids["alien"] = await repo.upsert_show(Show(title="Alien", year=1979, media_type=MediaType.MOVIE, ids={"tmdb": 348, "simkl": 112}, imdb_rating=8.5))
    ids["alien_tv"] = await repo.upsert_show(Show(title="Alien", year=2025, media_type=MediaType.SHOW, ids={"tmdb": 348, "simkl": 113}))
    ids["shogun"] = await repo.upsert_show(Show(title="Shōgun", year=2024, media_type=MediaType.SHOW, ids={"simkl": 114, "tvdb": 5000}, status=ShowStatus.WATCHING))
    return ids


async def add_source(ac, **over):
    res = await ac.post("/api/rating-sources", json={**SPEC, **over})
    assert res.status_code == 201, res.text
    return res.json()


def item(value, **match):
    return {"match": match, "value": value}


# --- sources ---------------------------------------------------------------------------------

async def test_sources_list_has_builtins_and_custom(library):
    async with client() as ac:
        await add_source(ac)
        got = (await ac.get("/api/rating-sources")).json()
    assert [s["id"] for s in got] == ["simkl", "imdb", "letterboxd"]
    assert got[0]["builtin"] and got[0]["read_only"] and got[0]["rated_titles"] == 1
    assert got[1]["rated_titles"] == 2 and got[1]["origin"] == "builtin"
    assert got[2]["origin"] == "api" and not got[2]["read_only"] and got[2]["scale_max"] == 5


@pytest.mark.parametrize("bad", [
    {"id": "Has Caps"}, {"id": "../x"}, {"id": ""}, {"id": "a" * 33}, {"id": "simkl"}, {"id": "imdb"}, {"id": "user"},
    {"scale_min": 5, "scale_max": 5}, {"scale_min": 6}, {"decimals": 4}, {"label": ""}, {"label": "toolonglabel"},
    {"name": "bad\x00name"}, {"name": "   "},
])
async def test_source_definitions_are_validated(library, bad):
    async with client() as ac:
        res = await ac.post("/api/rating-sources", json={**SPEC, **bad})
    assert res.status_code == 422, res.text


async def test_duplicate_source_is_a_conflict_and_edits_work(library):
    async with client() as ac:
        await add_source(ac)
        assert (await ac.post("/api/rating-sources", json=SPEC)).status_code == 409
        res = await ac.patch("/api/rating-sources/letterboxd", json={"label": "Box", "show_on_cards": False})
        assert res.status_code == 200 and res.json()["label"] == "Box" and not res.json()["show_on_cards"]
        assert (await ac.patch("/api/rating-sources/simkl", json={"label": "x"})).status_code == 409
        assert (await ac.patch("/api/rating-sources/nope", json={"label": "x"})).status_code == 404
        assert (await ac.patch("/api/rating-sources/Bad Id", json={"label": "x"})).status_code == 422


async def test_scale_cannot_shrink_below_stored_values(library):
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4.5, simkl=111)]})
        res = await ac.patch("/api/rating-sources/letterboxd", json={"scale_max": 4})
        assert res.status_code == 409 and "outside" in res.json()["detail"]
        assert (await ac.patch("/api/rating-sources/letterboxd", json={"scale_max": 6})).status_code == 200


# --- the import ------------------------------------------------------------------------------

async def test_import_matches_by_every_id_and_by_title(library):
    async with client() as ac:
        await add_source(ac)
        body = {"items": [
            item(4.0, simkl=111), item(4.5, imdb="tt0113277"),  # the same title twice: the last one wins
            item(3.0, tmdb=348, media_type="movie"), item(2.5, tvdb=5000),
            {"match": {"title": "shogun", "year": 2024}, "value": 5, "votes": 900, "url": "https://example.org/shogun"},
        ]}
        res = await ac.put("/api/ratings/letterboxd", json=body)
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["updated"] == 3 and out["unchanged"] == 0 and out["unmatched"] == [] and out["ambiguous"] == []
        heat = (await ac.get(f"/api/shows/{library['heat']}")).json()
        assert heat["ratings"]["letterboxd"]["value"] == 4.5
        shogun = (await ac.get(f"/api/shows/{library['shogun']}")).json()["ratings"]["letterboxd"]
        assert shogun["value"] == 5 and shogun["votes"] == 900 and shogun["url"] == "https://example.org/shogun"


async def test_import_reports_unmatched_ambiguous_and_unchanged(library):
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4.0, simkl=111)]})
        res = await ac.put("/api/ratings/letterboxd", json={"items": [
            item(4.0, simkl=111),                       # as stored
            item(3.0, simkl=999999),                    # no such title
            item(3.5, title="Alien"),                   # two titles are called Alien
            item(3.5, title="Alien", year=1979),
            item(1.0, title="Heat", year=1996),         # right name, wrong year
        ]})
        out = res.json()
    assert out["unchanged"] == 1 and out["updated"] == 1
    assert [u["index"] for u in out["unmatched"]] == [1, 4]
    assert out["unmatched"][0]["match"]["simkl"] == 999999
    assert [a["index"] for a in out["ambiguous"]] == [2]
    assert {c["id"] for c in out["ambiguous"][0]["candidates"]} == {library["alien"], library["alien_tv"]}


async def test_ambiguous_tmdb_id_is_resolved_by_media_type(library):
    async with client() as ac:
        await add_source(ac)
        both = (await ac.put("/api/ratings/letterboxd", json={"items": [item(3.0, tmdb=348)]})).json()
        one = (await ac.put("/api/ratings/letterboxd", json={"items": [item(3.0, tmdb=348, media_type="show")]})).json()
    assert len(both["ambiguous"]) == 1 and one["updated"] == 1 and not one["ambiguous"]


async def test_value_outside_the_scale_refuses_the_whole_batch(library):
    async with client() as ac:
        await add_source(ac)
        res = await ac.put("/api/ratings/letterboxd", json={"items": [item(4.0, simkl=111), item(5.5, simkl=112)]})
        assert res.status_code == 422
        assert res.json()["detail"][0]["loc"] == ["body", "items", 1, "value"]
        assert (await ac.get(f"/api/shows/{library['heat']}")).json()["ratings"] == {}  # nothing was written
        single = await ac.put(f"/api/shows/{library['heat']}/ratings/letterboxd", json={"value": -1})
        assert single.status_code == 422


@pytest.mark.parametrize("match", [
    {}, {"simkl": 1, "imdb": "tt1"}, {"title": "A", "simkl": 1}, {"year": 2000}, {"imdb": "nm0000"}, {"simkl": 0},
    {"title": ""}, {"title": "x", "media_type": "podcast"},
])
async def test_malformed_matches_are_422(library, match):
    async with client() as ac:
        await add_source(ac)
        res = await ac.put("/api/ratings/letterboxd", json={"items": [{"match": match, "value": 1}]})
    assert res.status_code == 422


@pytest.mark.parametrize("extra", [{"url": "javascript:alert(1)"}, {"url": "data:text/html,x"}, {"url": "//evil"},
                                   {"votes": -1}, {"value": "high"}])
async def test_unsafe_or_bad_item_fields_are_422(library, extra):
    async with client() as ac:
        await add_source(ac)
        res = await ac.put("/api/ratings/letterboxd", json={"items": [{"match": {"simkl": 111}, "value": 1, **extra}]})
    assert res.status_code == 422


async def test_batch_limit_is_1000_items(library):
    async with client() as ac:
        await add_source(ac)
        small = lambda n: {"items": [{"match": {"simkl": i + 5000}, "value": 1} for i in range(n)]}  # noqa: E731
        ok = await ac.put("/api/ratings/letterboxd", json=small(1000))
        assert ok.status_code == 200 and len(ok.json()["unmatched"]) == 1000
        too_many = await ac.put("/api/ratings/letterboxd", json=small(1001))
        assert too_many.status_code == 422
        assert (await ac.put("/api/ratings/letterboxd", json={"items": []})).status_code == 422


async def test_body_cap_applies_to_imports(library):
    async with client() as ac:
        await add_source(ac)
        res = await ac.put("/api/ratings/letterboxd", content=b'{"items": [' + b" " * (300 * 1024) + b"]}",
                           headers={"Content-Type": "application/json"})
    assert res.status_code == 413


async def test_unknown_builtin_and_bad_slugs_on_import(library):
    async with client() as ac:
        body = {"items": [item(1, simkl=111)]}
        assert (await ac.put("/api/ratings/nothing", json=body)).status_code == 404
        assert (await ac.put("/api/ratings/simkl", json=body)).status_code == 409
        assert (await ac.put("/api/ratings/Not%20A%20Slug", json=body)).status_code == 422
        assert (await ac.put("/api/ratings/UPPER", json=body)).status_code == 422
        assert (await ac.delete("/api/ratings/imdb")).status_code == 409


# --- values on titles ------------------------------------------------------------------------

async def test_single_value_details_and_delete(library):
    hid = library["heat"]
    async with client() as ac:
        await add_source(ac)
        put = await ac.put(f"/api/shows/{hid}/ratings/letterboxd", json={"value": 3.5, "votes": 10, "url": "https://example.org/h"})
        assert put.status_code == 200
        rows = {r["source"]: r for r in (await ac.get(f"/api/shows/{hid}/ratings")).json()}
        assert rows["letterboxd"]["value"] == 3.5 and rows["letterboxd"]["url"] == "https://example.org/h"
        assert rows["imdb"]["value"] == 8.3 and rows["imdb"]["builtin"] and rows["simkl"]["value"] == 8.0
        other = {r["source"]: r for r in (await ac.get(f"/api/shows/{library['alien']}/ratings")).json()}
        assert other["letterboxd"]["value"] is None  # listed even without a value
        assert (await ac.delete(f"/api/ratings/letterboxd/{hid}")).status_code == 204
        assert (await ac.delete(f"/api/ratings/letterboxd/{hid}")).status_code == 404
        assert (await ac.get("/api/shows/9999/ratings")).status_code == 404


async def test_clear_source_and_delete_source_cascade(library, setup_test_app):
    repo = setup_test_app
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111), item(3, simkl=112)]})
        cleared = await ac.delete("/api/ratings/letterboxd")
        assert cleared.json()["removed"] == 2
        assert (await ac.get("/api/rating-sources")).json()[2]["rated_titles"] == 0
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111)]})
        assert (await ac.delete("/api/rating-sources/letterboxd")).json()["removed_values"] == 1
    db = await repo.read_connection()
    assert (await (await db.execute("SELECT COUNT(*) FROM title_ratings")).fetchone())[0] == 0


async def test_deleting_a_title_removes_its_values(library, setup_test_app):
    repo = setup_test_app
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111)]})
    await repo.delete_show(library["heat"])
    db = await repo.read_connection()
    assert (await (await db.execute("SELECT COUNT(*) FROM title_ratings")).fetchone())[0] == 0


# --- using them ------------------------------------------------------------------------------

async def seeded(ac):
    await add_source(ac)
    await ac.put("/api/ratings/letterboxd", json={"items": [
        item(2.0, simkl=111), item(4.5, simkl=112), item(3.0, simkl=114)]})


async def test_sort_by_a_custom_source_puts_unrated_last(library):
    async with client() as ac:
        await seeded(ac)
        res = await ac.get("/api/shows", params={"sort_by": "rating:letterboxd"})
        titles = [(s["title"], s["year"]) for s in res.json()]
        assert titles[:3] == [("Alien", 1979), ("Shōgun", 2024), ("Heat", 1995)] and titles[3] == ("Alien", 2025)
        by_imdb = [s["title"] for s in (await ac.get("/api/shows", params={"sort_by": "rating:imdb"})).json()]
        assert by_imdb[:2] == ["Alien", "Heat"]
        by_simkl = [s["title"] for s in (await ac.get("/api/shows", params={"sort_by": "rating:simkl"})).json()]
        assert by_simkl[0] == "Heat"


async def test_sort_and_filter_refuse_unknown_sources(library):
    async with client() as ac:
        for params in ({"sort_by": "rating:nothing"}, {"min_rating": "nothing:3"}, {"min_rating": "letterboxd"},
                       {"min_rating": "letterboxd:abc"}, {"min_rating": "Bad Id:3"}, {"sort_by": "rating:x'; --"}):
            assert (await ac.get("/api/shows", params=params)).status_code == 422, params


async def test_min_rating_filters_and_combines(library):
    async with client() as ac:
        await seeded(ac)
        res = (await ac.get("/api/shows", params={"min_rating": "letterboxd:3"})).json()
        assert sorted(s["title"] for s in res) == ["Alien", "Shōgun"] and all(s["year"] != 2025 for s in res)
        both = (await ac.get("/api/shows", params=[("min_rating", "letterboxd:2"), ("min_rating", "imdb:8.4")])).json()
        assert [s["title"] for s in both] == ["Alien"]
        simkl = (await ac.get("/api/shows", params={"min_rating": "simkl:8"})).json()
        assert [s["title"] for s in simkl] == ["Heat"]


async def test_card_view_carries_only_the_sources_shown_on_cards(library):
    async with client() as ac:
        await seeded(ac)
        await add_source(ac, id="hidden", name="Hidden", label="HD", show_on_cards=False)
        await ac.put("/api/ratings/hidden", json={"items": [item(4, simkl=111)]})
        cards = {s["id"]: s for s in (await ac.get("/api/shows", params={"view": "card"})).json()}
        full = {s["id"]: s for s in (await ac.get("/api/shows")).json()}
    assert set(cards[library["heat"]]["ratings"]) == {"letterboxd"}
    assert set(full[library["heat"]]["ratings"]) == {"letterboxd", "hidden"}
    assert cards[library["alien_tv"]]["ratings"] == {}


async def test_csv_export_has_a_column_per_custom_source(library):
    async with client() as ac:
        await seeded(ac)
        await add_source(ac, id="neg", name="Net score", label="NET", scale_min=-5, scale_max=5)
        await ac.put("/api/ratings/neg", json={"items": [item(-2.5, simkl=111)]})
        text = (await ac.get("/api/export/library.csv")).text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert "partner_rating" not in rows[0]
    heat = next(r for r in rows if r["title"] == "Heat")
    assert heat["rating_letterboxd"] == "2.0" and heat["rating_neg"] == "-2.5"
    assert next(r for r in rows if r["title"] == "Shōgun")["rating_neg"] == ""


async def test_backup_carries_the_rating_tables(library, tmp_path):
    async with client() as ac:
        await seeded(ac)
        res = await ac.get("/api/export/backup.db")
    out = tmp_path / "b.db"
    out.write_bytes(res.content)
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT id, label FROM rating_sources").fetchall() == [("letterboxd", "LB")]
    assert conn.execute("SELECT COUNT(*) FROM title_ratings").fetchone()[0] == 3
    conn.close()


# --- config.yaml -----------------------------------------------------------------------------

async def test_config_sources_are_seeded_read_only_and_released_when_removed(library, setup_test_app):
    repo = setup_test_app
    spec = RatingSourceSpec(**{k: v for k, v in SPEC.items()})
    await repo.seed_rating_sources([spec])
    async with client() as ac:
        got = (await ac.get("/api/rating-sources")).json()[-1]
        assert got["origin"] == "config" and got["read_only"]
        assert (await ac.patch("/api/rating-sources/letterboxd", json={"label": "x"})).status_code == 409
        assert (await ac.delete("/api/rating-sources/letterboxd")).status_code == 409
        assert (await ac.post("/api/rating-sources", json=SPEC)).status_code == 409
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111)]})
        # the file changes the settings; the values stay
        await repo.seed_rating_sources([spec.model_copy(update={"name": "Letterboxd (films)", "label": "LBX"})])
        got = (await ac.get("/api/rating-sources")).json()[-1]
        assert got["label"] == "LBX" and got["rated_titles"] == 1
        # and when the file drops it, it becomes an ordinary source with its values
        await repo.seed_rating_sources([])
        got = (await ac.get("/api/rating-sources")).json()[-1]
        assert got["origin"] == "api" and not got["read_only"] and got["rated_titles"] == 1
        assert (await ac.patch("/api/rating-sources/letterboxd", json={"label": "LB"})).status_code == 200


def test_config_rejects_duplicate_and_reserved_ids():
    from pydantic import ValidationError
    from penguwatch.config import AppConfig
    with pytest.raises(ValidationError):
        AppConfig(ratings={"sources": [SPEC, SPEC]})
    with pytest.raises(ValidationError):
        AppConfig(ratings={"sources": [{**SPEC, "id": "imdb"}]})
    assert AppConfig(ratings={"sources": [SPEC]}).ratings.sources[0].id == "letterboxd"


# --- guards ----------------------------------------------------------------------------------

ROUTES = [
    ("GET", "/api/rating-sources", None),
    ("POST", "/api/rating-sources", SPEC),
    ("PATCH", "/api/rating-sources/letterboxd", {"label": "x"}),
    ("DELETE", "/api/rating-sources/letterboxd", None),
    ("PUT", "/api/ratings/letterboxd", {"items": [item(1, simkl=111)]}),
    ("DELETE", "/api/ratings/letterboxd", None),
    ("DELETE", "/api/ratings/letterboxd/1", None),
    ("GET", "/api/shows/1/ratings", None),
    ("PUT", "/api/shows/1/ratings/letterboxd", {"value": 1}),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_ratings_route_needs_the_token(library, token_mode, method, path, body):
    async with client() as ac:
        bare = await ac.request(method, path, json=body)
        assert bare.status_code == 401
        wrong = await ac.request(method, path, json=body, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        authed = await ac.request(method, path, json=body, headers=token_mode)
        assert authed.status_code != 401


@pytest.mark.parametrize("method,path,body", [r for r in ROUTES if r[0] != "GET"])
async def test_browser_writes_to_ratings_need_the_apps_origin(library, method, path, body):
    async with client() as ac:
        await add_source(ac)
        before = (await ac.get("/api/rating-sources")).json()
        blocked = await ac.request(method, path, json=body, headers={"Origin": "http://evil.example"})
        assert blocked.status_code == 403
        assert (await ac.get("/api/rating-sources")).json() == before  # nothing changed
        same = await ac.request(method, path, json=body, headers={"Origin": "http://test"})
        assert same.status_code != 403


# --- the security review's findings -------------------------------------------------------------------

async def test_a_bulk_import_raises_one_summary_event_not_one_per_title(library, setup_test_app):
    repo = setup_test_app
    got = []
    repo.add_event_listener(lambda t, d: got.append((t, d)))
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111), item(3, simkl=112), item(2, simkl=114)]})
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111)]})     # unchanged: nothing to say
        await ac.put(f"/api/shows/{library['heat']}/ratings/letterboxd", json={"value": 5})   # by hand: per title
    assert got[0] == ("ratings.imported", {"source": "letterboxd", "updated": 3, "unchanged": 0})
    assert [(t, d.get("to")) for t, d in got[1:]] == [("title.rating_changed", 5)]


async def test_removing_clearing_and_deleting_say_so(library, setup_test_app):
    repo = setup_test_app
    got = []
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111), item(3, simkl=112)]})
        repo.add_event_listener(lambda t, d: got.append((t, d)))
        await ac.delete(f"/api/ratings/letterboxd/{library['heat']}")
        await ac.delete("/api/ratings/letterboxd")
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111)]})
        await ac.delete("/api/rating-sources/letterboxd")
    assert got[0][0] == "title.rating_changed" and got[0][1]["from"] == 4 and got[0][1]["to"] is None
    assert got[1] == ("ratings.cleared", {"source": "letterboxd", "removed": 1, "source_deleted": False})
    assert got[2][0] == "ratings.imported"
    assert got[3] == ("ratings.cleared", {"source": "letterboxd", "removed": 1, "source_deleted": True})


async def test_the_scale_is_checked_inside_the_write_transaction(library, setup_test_app):
    from penguwatch.db.ratings import RatingOutOfScale, UnknownRatingSource
    from penguwatch.ratings import RatingValue
    repo = setup_test_app
    async with client() as ac:
        await add_source(ac)
    with pytest.raises(RatingOutOfScale):
        await repo.set_title_ratings("letterboxd", {library["heat"]: RatingValue(value=9)})   # as the API's own check would race
    with pytest.raises(UnknownRatingSource):
        await repo.set_title_ratings("gone", {library["heat"]: RatingValue(value=1)})
    assert (await repo.get_title_ratings(library["heat"])) == {}


async def test_the_scale_changing_during_an_import_is_a_422_not_a_stored_value(library, setup_test_app, monkeypatch):
    repo = setup_test_app
    async with client() as ac:
        await add_source(ac)
        real = repo.title_index

        async def narrowing():                      # the source's scale shrinks after the API's check, before the write
            await repo.update_rating_source("letterboxd", {"scale_max": 3})
            return await real()

        monkeypatch.setattr(repo, "title_index", narrowing)
        res = await ac.put("/api/ratings/letterboxd", json={"items": [item(4.5, simkl=111)]})
    assert res.status_code == 422 and isinstance(res.json()["detail"], list)
    assert await repo.get_title_ratings(library["heat"]) == {}


async def test_a_config_scale_cannot_narrow_below_stored_values(library, setup_test_app):
    repo = setup_test_app
    spec = RatingSourceSpec(**SPEC)
    await repo.seed_rating_sources([spec])
    async with client() as ac:
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4.5, simkl=111)]})
    with pytest.raises(ValueError, match="1 stored value.*outside"):
        await repo.seed_rating_sources([spec.model_copy(update={"scale_max": 3})])
    assert (await repo.get_rating_source("letterboxd")).scale_max == 5     # nothing changed
    await repo.seed_rating_sources([spec.model_copy(update={"scale_max": 10})])   # widening is fine


async def test_every_422_detail_is_a_list(library):
    async with client() as ac:
        await add_source(ac)
        cases = [
            await ac.get("/api/shows", params={"sort_by": "rating:nothing"}),
            await ac.get("/api/shows", params={"min_rating": "nothing:3"}),
            await ac.get("/api/shows", params={"min_rating": "bad"}),
            await ac.patch("/api/rating-sources/letterboxd", json={"scale_min": 9}),
            await ac.put(f"/api/shows/{library['heat']}/ratings/letterboxd", json={"value": 99}),
            await ac.put("/api/ratings/letterboxd", json={"items": [item(99, simkl=111)]}),
            await ac.put("/api/ratings/letterboxd", json={"items": []}),
        ]
    for res in cases:
        assert res.status_code == 422 and isinstance(res.json()["detail"], list), res.text
        assert {"type", "loc", "msg"} <= set(res.json()["detail"][0])


async def test_at_most_twenty_custom_sources(setup_test_app):
    async with client() as ac:
        for i in range(20):
            await add_source(ac, id=f"s{i}", name=f"S{i}", label=f"S{i}")
        res = await ac.post("/api/rating-sources", json={**SPEC, "id": "one-too-many"})
        assert res.status_code == 409 and "at most 20" in res.json()["detail"]
        await ac.delete("/api/rating-sources/s0")
        assert (await ac.post("/api/rating-sources", json={**SPEC, "id": "one-too-many"})).status_code == 201


async def test_list_shows_fallback_path_carries_ratings(library, setup_test_app, monkeypatch):
    """When a write lands between the id query and the parse, get_shows parses the rows directly."""
    repo = setup_test_app
    async with client() as ac:
        await add_source(ac)
        await ac.put("/api/ratings/letterboxd", json={"items": [item(4, simkl=111)]})
    async def stale(key, build):
        return {}                                   # the cached parse knows none of the ids: the fallback runs
    monkeypatch.setattr(repo, "derived", stale)
    shows = {s.id: s for s in await repo.get_shows()}
    assert shows[library["heat"]].ratings["letterboxd"].value == 4
