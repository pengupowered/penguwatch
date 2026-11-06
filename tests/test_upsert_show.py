"""Round-trip tests that pin what upsert_show writes on insert and on update."""

import pytest

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import HouseholdTag, MediaType, Priority, ServiceConfig, Show, ShowStatus

# Columns upsert_show never writes (set elsewhere): they must stay at their defaults.
NEVER_WRITTEN = {"episodes_synced_at", "sequels"}
VOLATILE = {"id", "created_at", "updated_at"}


def full_show(**over) -> Show:
    base = dict(
        ids={"simkl": 1001, "imdb": "tt1001", "tmdb": 2002, "tvdb": 3003}, media_type=MediaType.MOVIE,
        title="Round Trip", year=1999, poster="p.jpg", banner="b.jpg", overview="An overview.",
        genres=["Drama", "Mystery"], network="Some Net", service_id="netflix", service_manual=True,
        status=ShowStatus.WATCHING,
        priority=Priority.HIGH, household_tag=HouseholdTag.PERSON2, simkl_rating=7.7, imdb_rating=8.1,
        imdb_votes=12345, user_rating=9.0, total_episodes=24,
        watched_episodes=7, aired_episodes=20, runtime_minutes=52,
        next_episode_air_date="2030-01-02", next_episode_season=2, next_episode_number=5,
        next_episode_title="Next One", is_ended=True, last_watched="S01E07", next_to_watch="S01E08",
        notes="household note", last_watched_at="2026-01-01T10:00:00+00:00",
        enriched_at="2026-01-02T10:00:00+00:00",
    )
    base.update(over)
    return Show(**base)



async def make_repo(tmp_path) -> DatabaseRepository:
    repo = DatabaseRepository(str(tmp_path / "upsert.db"))
    await repo.initialize([
        ServiceConfig(id="netflix", name="Netflix", monthly_cost=10.0),
        ServiceConfig(id="max", name="Max", monthly_cost=12.0, aliases=["HBO"]),
    ])
    return repo


@pytest.mark.asyncio
async def test_insert_round_trips_every_column(tmp_path):
    repo = await make_repo(tmp_path)
    try:
        show = full_show()
        show_id = await repo.upsert_show(show)
        got = await repo.get_show_by_id(show_id)
        assert got is not None and got.id == show_id
        assert got.created_at and got.updated_at
        expected = full_show().model_dump()
        actual = got.model_dump()
        for field in expected:
            if field in VOLATILE:
                continue
            assert actual[field] == expected[field], field
        for field in NEVER_WRITTEN:
            assert getattr(got, field) in (None, [])
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_update_coalesces_and_keeps_household_fields(tmp_path):
    repo = await make_repo(tmp_path)
    try:
        show_id = await repo.upsert_show(full_show())
        changed = Show(
            ids={"simkl": 1001}, title="Renamed", poster="new.jpg", genres=[], status=ShowStatus.COMPLETED,
            watched_episodes=3, imdb_rating=5.5, is_ended=False,
            # household-only values that must NOT overwrite the stored ones
            priority=Priority.LOW, household_tag=HouseholdTag.BOTH,
            notes="other note", service_manual=False, enriched_at="2000-01-01",
            # total/aired 0 keep the stored counts; runtime default 45 is a real value
            total_episodes=0, aired_episodes=0,
        )
        assert await repo.upsert_show(changed) == show_id
        got = await repo.get_show_by_id(show_id)
        assert got is not None
        # plain / replaced
        assert got.title == "Renamed" and got.genres == []
        assert got.status == ShowStatus.COMPLETED and got.watched_episodes == 3
        assert got.is_ended is False
        # COALESCE: None keeps the old value, a value replaces it
        assert got.year == 1999 and got.ids.get("imdb") == "tt1001" and got.ids.get("tmdb") == 2002
        assert got.overview == "An overview." and got.network == "Some Net"
        assert got.poster == "new.jpg" and got.imdb_rating == 5.5
        assert got.user_rating == 9.0 and got.simkl_rating == 7.7
        assert got.next_episode_title == "Next One" and got.last_watched == "S01E07"
        assert got.next_to_watch == "S01E08" and got.last_watched_at == "2026-01-01T10:00:00+00:00"
        assert got.runtime_minutes == 45
        assert got.media_type == MediaType.SHOW  # the default replaces: media_type is always set
        # zero keeps counts
        assert got.total_episodes == 24 and got.aired_episodes == 20
        # household-only fields untouched
        assert got.priority == Priority.HIGH and got.household_tag == HouseholdTag.PERSON2
        assert got.notes == "household note"
        assert got.service_manual is True
        assert got.enriched_at == "2026-01-02T10:00:00+00:00"
        assert got.service_id == "netflix"  # manual pick wins over the incoming None/match
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_update_positive_counts_and_service_when_not_manual(tmp_path):
    repo = await make_repo(tmp_path)
    try:
        show_id = await repo.upsert_show(full_show(service_manual=False))
        await repo.upsert_show(Show(id=show_id, title="T", service_id="max", total_episodes=30, aired_episodes=28))
        got = await repo.get_show_by_id(show_id)
        assert got is not None
        assert got.service_id == "max"
        assert got.total_episodes == 30 and got.aired_episodes == 28
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_insert_with_missing_media_type_stores_show(tmp_path):
    repo = await make_repo(tmp_path)
    try:
        show = Show(ids={"simkl": 77}, title="No Type")
        show.media_type = None  # type: ignore[assignment]
        show_id = await repo.upsert_show(show)
        got = await repo.get_show_by_id(show_id)
        assert got is not None and got.media_type == MediaType.SHOW
    finally:
        await repo.close()
