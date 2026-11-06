"""English titles and forgiving search: migration 0007, every write path keeping search_text current,
capture from SIMKL details, the one-time backfill and the API."""

import json
from datetime import timedelta

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import clock
from penguwatch.db.migrations import MIGRATIONS, run_migrations
from penguwatch.db.repository import DatabaseRepository
from penguwatch.main import app
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.drivers.simkl.mapper import parse_show_item, title_names
from penguwatch.sync import enrich

from tests.test_migrations import LEGACY_SQL
from tests.test_simkl_politeness import add_shows, env, no_spacing  # noqa: F401  (fixtures + helper)


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "titles.db"))
    await r.initialize()
    yield r
    await r.close()


async def raw(repo, show_id, column):
    db = await repo.get_connection()
    cur = await db.execute(f"SELECT {column} FROM shows WHERE id = ?", (show_id,))
    return (await cur.fetchone())[0]


async def found(repo, query):
    return sorted(s.title for s in await repo.get_shows(search_query=query))


# ------------------------------------------------------------------ migration

async def test_migration_adds_columns_and_fills_search_text_for_existing_rows(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as conn:
        await conn.executescript(LEGACY_SQL)
        await conn.execute(
            "INSERT INTO shows (simkl_id, title, network, genres_json) VALUES (1, 'Shōgun', 'Obscure Net', '[\"Drama\"]')"
        )
        await conn.commit()
        assert (await run_migrations(conn))[-1] == len(MIGRATIONS)
        cur = await conn.execute("SELECT en_title, alt_titles_json, search_text FROM shows")
        en_title, alt, text = await cur.fetchone()
    assert en_title is None and alt is None  # alt NULL = never checked
    assert text.split("\n\n")[0].split("\n") == ["shogun", "obscure net", "drama"]


# ------------------------------------------------------------------ every write path keeps search_text current

async def test_upsert_insert_and_update_keep_search_text_current(repo):
    sid = await repo.upsert_show(Show(title="Shōgun", ids={"simkl": 1}, network="Obscure Net", genres=["Drama"]))
    assert await found(repo, "shogun") == ["Shōgun"]
    assert await found(repo, "obscure net") == ["Shōgun"]
    await repo.upsert_show(Show(title="Shōgun 2024", ids={"simkl": 1}, genres=["Drama", "History"],
                                en_title="Shogun: The Series", alt_titles=["将軍"]))
    assert await found(repo, "shogun 2024") == ["Shōgun 2024"]
    assert await found(repo, "history") == ["Shōgun 2024"]
    assert await found(repo, "将軍") == ["Shōgun 2024"]
    assert (await repo.get_show_by_id(sid)).en_title == "Shogun: The Series"
    assert (await repo.get_show_by_id(sid)).alt_titles == ["将軍"]


async def test_upsert_without_names_keeps_the_stored_ones(repo):
    sid = await repo.upsert_show(Show(title="Sousou no Frieren", ids={"simkl": 2}, en_title="Frieren", alt_titles=["Frieren 2"]))
    await repo.upsert_show(Show(title="Sousou no Frieren", ids={"simkl": 2}))
    show = await repo.get_show_by_id(sid)
    assert show.en_title == "Frieren" and show.alt_titles == ["Frieren 2"]


async def test_update_show_fields_refreshes_only_when_a_source_column_changes(repo):
    sid = await repo.upsert_show(Show(title="Alpha", ids={"simkl": 3}))
    await repo.update_show_fields(sid, {"network": "Crunchyroll"})
    assert await found(repo, "crunchyroll") == ["Alpha"]
    await repo.update_show_fields(sid, {"title": "Beta"})
    assert await found(repo, "alpha") == [] and await found(repo, "beta") == ["Beta"]
    await repo.update_show_fields(sid, {"genres_json": json.dumps(["Mecha"])})
    assert await found(repo, "mecha") == ["Beta"]
    await repo.update_show_fields(sid, {"en_title": "Gamma Show"})
    assert await found(repo, "gamma") == ["Beta"]
    await repo.update_show_fields(sid, {"alt_titles_json": json.dumps(["Delta"])})
    assert await found(repo, "delta") == ["Beta"]
    before = await raw(repo, sid, "search_text")
    await repo.update_show_fields(sid, {"notes": "x"})
    assert await raw(repo, sid, "search_text") == before


async def test_update_show_metadata_network_refreshes_search_text(repo):
    sid = await repo.upsert_show(Show(title="Alpha", ids={"simkl": 4}))
    await repo.update_show_metadata(sid, network="HIDIVE")
    assert await found(repo, "hidive") == ["Alpha"]


async def test_update_show_titles(repo):
    sid = await repo.upsert_show(Show(title="Sousou no Frieren", ids={"simkl": 5}))
    assert await raw(repo, sid, "alt_titles_json") is None
    await repo.update_show_titles(sid, "Frieren: Beyond Journey's End", ["Frieren", "Frieren"])
    show = await repo.get_show_by_id(sid)
    assert show.en_title == "Frieren: Beyond Journey's End" and show.alt_titles == ["Frieren"]
    assert await found(repo, "beyond journey's end") == ["Sousou no Frieren"]
    await repo.update_show_titles(sid, "sousou no frieren", [])  # restates the title: not stored; [] = checked
    assert (await repo.get_show_by_id(sid)).en_title is None
    assert await raw(repo, sid, "alt_titles_json") == "[]"


async def test_new_rows_always_get_search_text(repo):
    sid = await repo.upsert_show(Show(title="Plain", ids={"simkl": 6}))
    assert await raw(repo, sid, "search_text")


# ------------------------------------------------------------------ search

async def test_search_is_forgiving_and_covers_all_names(repo):
    await repo.upsert_show(Show(title="Shōgun", ids={"simkl": 10}, network="Obscure Net"))
    await repo.upsert_show(Show(title="Re:Zero", ids={"simkl": 11}, genres=["Anime"], media_type=MediaType.ANIME,
                                en_title="Re:Zero - Starting Life in Another World", alt_titles=["Re Zero kara"]))
    await repo.upsert_show(Show(title="Pokémon", ids={"simkl": 12}))
    await repo.upsert_show(Show(title="John Wick", ids={"simkl": 13}))
    for q in ("shogun", "shougun", "shoogun", "SHŌGUN"):
        assert await found(repo, q) == ["Shōgun"], q
    assert await found(repo, "pokemon") == ["Pokémon"]
    assert await found(repo, "re zero") == ["Re:Zero"]
    assert await found(repo, "rezero") == ["Re:Zero"]
    assert await found(repo, "starting life in another") == ["Re:Zero"]
    assert await found(repo, "kara") == ["Re:Zero"]
    assert await found(repo, "obscure net") == ["Shōgun"]
    assert await found(repo, "john") == ["John Wick"]
    assert await found(repo, "jon") == []
    assert await found(repo, "Shōgun") == ["Shōgun"]  # the old raw match still works
    assert await found(repo, "!!!") == []  # nothing searchable left: only the raw match, which finds nothing


async def test_search_endpoint(container):
    await container.repository.upsert_show(Show(title="Shōgun", ids={"simkl": 20}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/shows", params={"search": "shogun"})
        assert [s["title"] for s in res.json()] == ["Shōgun"]


# ------------------------------------------------------------------ API shape

async def test_api_carries_en_title_and_alt_titles(container):
    repo = container.repository
    sid = await repo.upsert_show(Show(title="Sousou no Frieren", ids={"simkl": 30}, en_title="Frieren: Beyond Journey's End",
                                      alt_titles=["Frieren"], overview="o"))
    plain = await repo.upsert_show(Show(title="Plain", ids={"simkl": 31}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        full = (await ac.get(f"/api/shows/{sid}")).json()
        assert full["en_title"] == "Frieren: Beyond Journey's End" and full["alt_titles"] == ["Frieren"]
        card = {s["id"]: s for s in (await ac.get("/api/shows", params={"view": "card"})).json()}
        assert card[sid]["en_title"] == "Frieren: Beyond Journey's End"
        assert "alt_titles" not in card[sid] and "overview" not in card[sid]
        assert card[plain]["en_title"] is None  # the card payload always carries the key


# ------------------------------------------------------------------ capture from SIMKL

def test_title_names_from_details():
    show = Show(title="Sousou no Frieren", ids={"simkl": 1})
    details = {"en_title": "Frieren: Beyond Journey's End",
               "alt_titles": [{"name": "Frieren", "lang": 1, "type": "official"}, {"name": "Frieren"}, {"name": "葬送のフリーレン"}]}
    assert title_names(show, details) == ("Frieren: Beyond Journey's End", ["Frieren", "葬送のフリーレン"])
    assert title_names(show, {"overview": "x"}) == (None, [])  # checked, nothing found
    assert title_names(show, {"en_title": "SOUSOU no frieren"}) == (None, [])
    assert title_names(show, {}) is None  # details missing: stays unchecked


def test_list_items_carry_names_when_present():
    item = {"show": {"title": "Sousou no Frieren", "en_title": "Frieren", "alt_titles": [{"name": "F"}],
                     "ids": {"simkl": 1}}, "status": "watching"}
    show = parse_show_item(item, is_anime=True)
    assert show.en_title == "Frieren" and show.alt_titles == ["F"]
    assert parse_show_item({"show": {"title": "X", "ids": {"simkl": 2}}}).en_title is None


async def test_enrichment_stores_the_names(env, no_spacing):  # noqa: F811
    repo, client, sync = env
    (sid,) = await add_shows(repo, 1, status=ShowStatus.WATCHING, media_type=MediaType.ANIME)

    async def get_details(media, simkl_id):
        return {"overview": "x", "en_title": "English Name", "alt_titles": [{"name": "Other"}]}

    client.get_details = get_details
    assert await sync.enrich_titles([sid]) == 1
    show = await repo.get_show_by_id(sid)
    assert show.en_title == "English Name" and show.alt_titles == ["Other"]
    assert await found(repo, "english name") == ["Show 0"]


async def test_failed_details_leave_the_title_unchecked(env, no_spacing):  # noqa: F811
    repo, client, sync = env
    (sid,) = await add_shows(repo, 1, status=ShowStatus.WATCHING, media_type=MediaType.ANIME)

    async def get_details(media, simkl_id):
        return {}

    client.get_details = get_details
    await sync.enrich_titles([sid])
    assert await raw(repo, sid, "alt_titles_json") is None


# ------------------------------------------------------------------ backfill

@pytest.fixture
def now(monkeypatch):
    holder = {"t": clock.utc_now()}
    monkeypatch.setattr(enrich, "_utcnow", lambda: holder["t"])
    return holder


async def test_unchecked_titles_are_due_regardless_of_enriched_at_anime_first(env, no_spacing, now):  # noqa: F811
    repo, client, sync = env
    shows = await add_shows(repo, 3, first_id=100, status=ShowStatus.COMPLETED, media_type=MediaType.SHOW)
    anime = await add_shows(repo, 2, first_id=200, status=ShowStatus.COMPLETED, media_type=MediaType.ANIME)
    stamp = now["t"].isoformat()
    for sid in shows + anime:  # all freshly enriched: the 14-day rule alone would not touch them
        await repo.update_show_fields(sid, {"enriched_at": stamp})
    requested = []

    async def get_details(media, simkl_id):
        requested.append(simkl_id)
        return {"overview": "x", "alt_titles": []}

    client.get_details = get_details
    assert await sync.enrich_titles(limit=3) == 3
    assert requested[:2] == [200, 201]  # anime first
    assert await sync.enrich_titles(limit=25) == 2  # the rest, once
    assert await sync.enrich_titles(limit=25) == 0  # everything checked; fresh enriched_at: nothing due


async def test_backfill_takes_titles_being_watched_first(env, no_spacing, now):  # noqa: F811
    """Up Next shows titles being watched, so they get their English name before the rest of the library."""
    repo, client, sync = env
    done_anime = await add_shows(repo, 2, first_id=400, status=ShowStatus.COMPLETED, media_type=MediaType.ANIME)
    watching_show = await add_shows(repo, 1, first_id=500, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    watching_anime = await add_shows(repo, 1, first_id=600, status=ShowStatus.WATCHING, media_type=MediaType.ANIME)
    for sid in done_anime + watching_show + watching_anime:
        await repo.update_show_fields(sid, {"enriched_at": now["t"].isoformat()})
    requested = []

    async def get_details(media, simkl_id):
        requested.append(simkl_id)
        return {"overview": "x", "alt_titles": []}

    client.get_details = get_details
    assert await sync.enrich_titles(limit=2) == 2
    assert requested == [600, 500]  # watching anime, then watching show, before completed anime


async def test_backfill_respects_back_off_and_logs_remainder(env, no_spacing, now, caplog):  # noqa: F811
    repo, client, sync = env
    ids = await add_shows(repo, 4, first_id=300, status=ShowStatus.COMPLETED, media_type=MediaType.ANIME)
    for sid in ids:
        await repo.update_show_fields(sid, {"enriched_at": now["t"].isoformat()})

    async def get_details(media, simkl_id):
        return {} if simkl_id == 300 else {"overview": "x"}

    client.get_details = get_details
    with caplog.at_level("INFO", logger="penguwatch.sync"):
        assert await sync.enrich_titles(limit=2) == 2
    assert "2 titles still to fetch" in caplog.text
    assert await sync.enrich_titles(limit=25) == 2  # 300 is backed off for 24 h
    now["t"] += timedelta(hours=25)
    assert await sync.enrich_titles(limit=25) == 1


# ------------------------------------------------------------------ add-show search

async def test_simkl_search_retries_once_with_the_plain_spelling(container, monkeypatch):
    calls = []

    async def search(q, kinds=None, **kw):
        calls.append(q)
        return [{"title": "Shogun", "ids": {"simkl": 1}, "media_type": "show"}] if q == "Shogun" else []

    container.drivers.get("simkl").client.client_id = "cid"
    monkeypatch.setattr(container.drivers.get("simkl").client, "search", search)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/catalog/search", params={"q": "Shōgun"})
        assert [r["title"] for r in res.json()] == ["Shogun"] and calls == ["Shōgun", "Shogun"]
        calls.clear()
        assert (await ac.get("/api/catalog/search", params={"q": "nothing"})).json() == [] and calls == ["nothing"]
        calls.clear()
        assert (await ac.get("/api/catalog/search", params={"q": "葬送"})).json() == [] and calls == ["葬送"]


async def test_simkl_search_results_carry_the_english_title(container, monkeypatch):
    async def search(q, kinds=None, **kw):
        return [{"title": "Kusuriya no Hitorigoto", "title_en": "The Apothecary Diaries", "ids": {"simkl": 1},
                 "media_type": "anime"},
                {"title": "Shy", "title_en": "SHY", "ids": {"simkl": 2}, "media_type": "anime"}]

    container.drivers.get("simkl").client.client_id = "cid"
    monkeypatch.setattr(container.drivers.get("simkl").client, "search", search)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = (await ac.get("/api/catalog/search", params={"q": "kusuriya"})).json()
    assert res[0]["en_title"] == "The Apothecary Diaries" and "en_title" not in res[1]


async def test_adding_a_title_stores_its_english_title(container, monkeypatch):
    async def get_details(media, simkl_id):
        return {"title": "Kusuriya no Hitorigoto", "en_title": "The Apothecary Diaries",
                "alt_titles": [{"name": "Drugstore Soliloquy"}], "ids": {"simkl": simkl_id}}

    container.drivers.get("simkl").client.client_id = "cid"
    monkeypatch.setattr(container.drivers.get("simkl").client, "get_details", get_details)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        created = (await ac.post("/api/shows", json={"title": "Kusuriya no Hitorigoto", "ids": {"simkl": 55},
                                                     "media_type": "anime"})).json()
    assert created["en_title"] == "The Apothecary Diaries"
    stored = await container.repository.get_show_by_id(created["id"])
    assert stored.alt_titles == ["Drugstore Soliloquy"]


def test_add_dialog_shows_the_english_title():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static" / "dialogs" / "add-title" / "dialog.js")
    assert "displayTitle(item)" in js.read_text(encoding="utf-8")


# ------------------------------------------------------------------ other surfaces

async def test_up_next_tidy_schedule_and_planner_carry_the_english_title(container):
    repo = container.repository
    sid = await repo.upsert_show(Show(
        title="Sousou no Frieren", ids={"simkl": 40}, en_title="Frieren: Beyond Journey's End", status=ShowStatus.WATCHING,
        next_episode_air_date=(clock.today() + timedelta(days=2)).isoformat(), next_episode_season=1,
        next_episode_number=5, aired_episodes=4, watched_episodes=4,
    ))
    plain = await repo.upsert_show(Show(title="Plain", ids={"simkl": 41}, status=ShowStatus.WATCHING))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        up = {s["id"]: s for s in (await ac.get("/api/up-next")).json()}
        assert up[sid]["en_title"] == "Frieren: Beyond Journey's End"
        cal = {s["id"]: s for s in (await ac.get("/api/calendar/upcoming")).json()}
        assert cal[sid]["en_title"] == "Frieren: Beyond Journey's End"
        ics = (await ac.get("/api/calendar.ics")).text
        assert "SUMMARY:Frieren: Beyond Journey's End (Sousou no Frieren) S01E05" in ics.replace("\\,", ",")
        tidy = (await ac.get("/api/tidy")).json()
        assert tidy is not None
    assert "en_title" not in up[plain]
    assert await repo.get_en_titles([sid, plain]) == {sid: "Frieren: Beyond Journey's End"}
    assert await repo.get_en_titles([]) == {}


def test_feed_title_is_english_first():
    from penguwatch.api.schedule import feed_title
    assert feed_title("Sousou no Frieren", "Frieren") == "Frieren (Sousou no Frieren)"
    assert feed_title("Severance", None) == "Severance" and feed_title("Same", "Same") == "Same"


def test_rotation_pick_omits_unknown_english_title():
    from penguwatch.models import RotationPick
    assert "en_title" not in RotationPick(show_id=1, title="A").model_dump()
    assert RotationPick(show_id=1, title="A", en_title="B").model_dump()["en_title"] == "B"


def test_frontend_uses_the_shared_title_helper():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static"
    util = (static / "js" / "util.js").read_text(encoding="utf-8")
    assert "export function displayTitle" in util and "export function subtitleHtml" in util
    for name in ("panels/watchlist/cards", "dialogs/details/dialog", "panels/upnext/panel", "panels/decide/panel",
                 "panels/watchlist/tidy", "panels/watchlist/tidyassign", "panels/optimizer/planner", "panels/calendar/panel"):
        assert "displayTitle" in (static / f"{name}.js").read_text(encoding="utf-8"), name
