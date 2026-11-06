"""get_shows reuses the parsed library until the next write; callers get copies, filters and order still apply."""

import pytest

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Priority, Show, ShowStatus


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "cache.db"))
    await r.initialize([])
    yield r
    await r.close()


async def _add(repo, simkl_id, title, status=ShowStatus.WATCHING, priority=Priority.MEDIUM):
    return await repo.upsert_show(Show(ids={"simkl": simkl_id}, title=title, status=status, priority=priority))


@pytest.mark.asyncio
async def test_second_read_reuses_parsed_shows(repo, monkeypatch):
    await _add(repo, 1, "Alpha")
    await _add(repo, 2, "Beta")
    first = await repo.get_shows()
    parsed = []
    original = repo._row_to_show
    monkeypatch.setattr(repo, "_row_to_show", lambda r: parsed.append(r) or original(r))
    second = await repo.get_shows()
    assert parsed == []
    assert [s.model_dump() for s in second] == [s.model_dump() for s in first]


@pytest.mark.asyncio
async def test_a_write_is_visible_on_the_next_read(repo):
    await _add(repo, 1, "Alpha")
    assert [s.title for s in await repo.get_shows()] == ["Alpha"]
    await _add(repo, 2, "Beta")
    shows = await repo.get_shows(sort_by="title")
    assert [s.title for s in shows] == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_filters_and_order_come_from_the_query(repo):
    await _add(repo, 1, "Zeta", priority=Priority.LOW)
    await _add(repo, 2, "Alpha", status=ShowStatus.COMPLETED)
    await _add(repo, 3, "Mid", priority=Priority.HIGH)
    await repo.get_shows()  # fill the cache
    assert [s.title for s in await repo.get_shows(sort_by="title")] == ["Alpha", "Mid", "Zeta"]
    assert [s.title for s in await repo.get_shows()] == ["Mid", "Alpha", "Zeta"]  # priority, then title
    assert [s.title for s in await repo.get_shows(status=ShowStatus.COMPLETED)] == ["Alpha"]


@pytest.mark.asyncio
async def test_changing_a_returned_show_does_not_leak_into_later_reads(repo):
    await _add(repo, 1, "Alpha")
    shows = await repo.get_shows()
    shows[0].interest_score = 99
    shows[0].title = "Mutated"
    again = await repo.get_shows()
    assert again[0].title == "Alpha"
    assert again[0].interest_score != 99


# --- the unvalidated parse and the card shape give what the validated model gave ---------------------------------

async def _varied_library(repo):
    """Titles with every kind of stored value the parse sees: odd-case and padded ids, alt titles, sequels, ratings."""
    from penguwatch.models import MediaType
    from penguwatch.ratings import RatingSourceSpec
    await repo.create_rating_source(RatingSourceSpec(id="critics", name="Critics", label="CR", show_on_cards=True,
                                                     scale_min=0, scale_max=100, decimals=0))
    await repo.create_rating_source(RatingSourceSpec(id="friends", name="Friends", label="FR", show_on_cards=False,
                                                     scale_min=0, scale_max=100, decimals=0))
    a = await repo.upsert_show(Show(ids={"simkl": 1, "imdb": "tt0000001"}, title="Alpha", year=2020, status=ShowStatus.WATCHING,
                                    priority=Priority.HIGH, genres=["Drama", "Sci-Fi"], aired_episodes=10, watched_episodes=4,
                                    imdb_rating=8, user_rating=7.5, poster="p.jpg", network="HBO", next_to_watch="S01E05"))
    b = await repo.upsert_show(Show(ids={"simkl": 2}, title="Beta", media_type=MediaType.ANIME, status=ShowStatus.COMPLETED,
                                    is_ended=True, en_title="Beta (English)", aired_episodes=12, watched_episodes=12))
    c = await repo.upsert_show(Show(ids={"simkl": 3}, title="Film", media_type=MediaType.MOVIE, status=ShowStatus.PLANTOWATCH,
                                    aired_episodes=1))
    await repo.update_show_fields(b, {"alt_titles_json": '["Bee", "Bee", "  ", "B2"]',
                                      "sequels_json": '[{"ids": {"simkl": 9}, "title": "Beta 2", "year": 2027}]'})
    await repo.update_show_fields(c, {"service_manual": 1, "notes": "n", "runtime_minutes": 100})
    db = await repo.get_connection()
    # ids as an older version or a hand edit may have left them
    await db.execute("INSERT OR REPLACE INTO title_ids (show_id, provider, external_id) VALUES (?, ' TMDB ', ?)", (a, " 1396 "))
    await db.execute("INSERT OR REPLACE INTO title_ids (show_id, provider, external_id) VALUES (?, 'tvdb', ?)", (a, "081189"))
    await db.executemany("INSERT INTO title_ratings (show_id, source_id, value, votes, url, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                         [(a, "critics", 88, 120, "http://x", "2026-01-01"), (a, "friends", 4, None, None, "2026-01-02"),
                          (b, "friends", 90, None, None, "2026-01-02")])
    await db.commit()
    repo._data_version += 1
    return a, b, c


@pytest.mark.asyncio
async def test_the_unvalidated_parse_equals_the_validated_one(repo):
    await _varied_library(repo)
    cached = await repo.derived("shows_by_id", repo._parse_all_shows)
    db = await repo.read_connection()
    from penguwatch.db.shows import title_ids_tx
    all_ids = await title_ids_tx(db)
    ratings = await repo._all_title_ratings()
    rows = await (await db.execute("SELECT * FROM shows")).fetchall()
    assert len(rows) == len(cached) == 3
    for r in rows:
        validated = repo._row_to_show(r, all_ids.get(r["id"]))
        validated.ratings = ratings.get(r["id"], {})
        assert cached[r["id"]].model_dump(mode="json") == validated.model_dump(mode="json")
        assert cached[r["id"]].model_dump() == validated.model_dump()
        assert cached[r["id"]] == validated
    ids = cached[min(cached)].ids
    assert ids["tmdb"] == 1396 and ids["tvdb"] == "081189"  # normalised before the unvalidated build


@pytest.mark.asyncio
async def test_card_dicts_equal_the_model_dump_of_the_card_fields(repo):
    from penguwatch.api.shows import CARD_FIELDS, _card
    await _varied_library(repo)
    on_cards = {"critics"}
    for shows in (await repo.get_shows(), await repo.get_shows(sort_by="title")):
        for with_score in (False, True):
            for s in shows:
                if with_score:
                    s.interest_score, s.interest_reasons = 42, ["Drama"]
                old = s.model_dump(mode="json", include=CARD_FIELDS)
                old["ratings"] = {k: v for k, v in old["ratings"].items() if k in on_cards}
                new = _card(s, on_cards)
                assert new == old
                assert list(new) == list(old)  # also the key order of the JSON
    assert any(_card(s, on_cards)["ratings"] for s in await repo.get_shows())
