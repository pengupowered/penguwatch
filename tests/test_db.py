"""Tests for Database repository."""

import pytest
from datetime import date, timedelta
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show, ServiceConfig, ShowStatus, Priority


@pytest.mark.asyncio
async def test_repository_init_and_services(tmp_path):
    db_file = str(tmp_path / "test.db")
    repo = DatabaseRepository(db_file)
    default_services = [
        ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV", "Apple"]),
        ServiceConfig(id="max", name="Max", monthly_cost=15.99, aliases=["HBO", "HBO Max"]),
    ]
    await repo.initialize(default_services)

    services = await repo.get_services()
    assert len(services) == 2
    apple = await repo.get_service("apple_tv")
    assert apple is not None
    assert apple.name == "Apple TV+"

    # Match service by alias
    matched_id = await repo.match_service_by_network("HBO")
    assert matched_id == "max"

    matched_id_apple = await repo.match_service_by_network("Apple TV+")
    assert matched_id_apple == "apple_tv"

    await repo.close()


@pytest.mark.asyncio
async def test_repository_show_crud_and_progress(tmp_path):
    db_file = str(tmp_path / "test.db")
    repo = DatabaseRepository(db_file)
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99)])

    show = Show(
        ids={"simkl": 101},
        title="Severance",
        year=2022,
        network="Apple TV+",
        status=ShowStatus.WATCHING,
        priority=Priority.HIGH,
        household=["person1", "person2"],
        imdb_rating=8.7,
        total_episodes=10,
        aired_episodes=9,
        watched_episodes=4,
    )
    show_id = await repo.upsert_show(show)
    assert show_id > 0

    fetched = await repo.get_show_by_id(show_id)
    assert fetched is not None
    assert fetched.title == "Severance"
    assert fetched.service_id == "apple_tv"
    assert fetched.watched_episodes == 4
    assert fetched.episodes_left == 5

    # Increment episode
    updated = await repo.increment_show_episode(show_id)
    assert updated.watched_episodes == 5
    assert updated.episodes_left == 4

    # Update metadata
    updated_meta = await repo.update_show_metadata(show_id, priority=Priority.MEDIUM)
    assert updated_meta.priority == Priority.MEDIUM

    # Stats
    stats = await repo.get_dashboard_stats()
    assert stats["watching_count"] == 1
    assert stats["shared_count"] == 1

    # Delete
    deleted = await repo.delete_show(show_id)
    assert deleted is True
    assert (await repo.get_show_by_id(show_id)) is None

    await repo.close()


@pytest.mark.asyncio
async def test_completed_shows_exclusion_and_schedule(tmp_path):
    db_file = str(tmp_path / "test2.db")
    repo = DatabaseRepository(db_file)
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, is_active=True)])

    # Completed show
    completed_show = Show(
        ids={"simkl": 201},
        title="Ted Lasso Complete",
        network="Apple TV+",
        status=ShowStatus.COMPLETED,
        total_episodes=34,
        aired_episodes=34,
        watched_episodes=30,  # Even if watched < aired on SIMKL, completed status means 0 left
        runtime_minutes=30,
    )
    assert completed_show.episodes_left == 0
    assert completed_show.is_caught_up is True
    assert completed_show.is_ready_to_binge is False

    cid = await repo.upsert_show(completed_show)

    # Watching show
    watching_show = Show(
        ids={"simkl": 202},
        title="Slow Horses",
        network="Apple TV+",
        status=ShowStatus.WATCHING,
        total_episodes=36,
        aired_episodes=30,
        watched_episodes=28,
        runtime_minutes=45,
    )
    assert watching_show.episodes_left == 2
    wid = await repo.upsert_show(watching_show)

    # Stats should count 1 completed, 1 watching, and only 2 unwatched episodes (1.5 hrs)
    stats = await repo.get_dashboard_stats()
    assert stats["total_shows"] == 2
    assert stats["completed_count"] == 1
    assert stats["watching_count"] == 1
    assert stats["total_unwatched_episodes"] == 2
    assert round(stats["total_backlog_hours"], 1) == 1.5

    # Upsert upcoming episodes for Slow Horses
    await repo.upsert_upcoming_episodes([
        {
            "show_id": wid,
            "simkl_id": 202,
            "season": 6,
            "number": 3,
            "title": "Resurrection",
            "air_date": (date.today() + timedelta(days=3)).isoformat(),
        }
    ])

    sched = await repo.get_upcoming_schedule(days_ahead=45)
    assert len(sched) >= 1
    assert sched[0]["title"] == "Slow Horses"
    assert sched[0]["next_episode_season"] == 6
    assert sched[0]["next_episode_number"] == 3

    await repo.close()
