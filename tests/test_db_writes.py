"""Write transactions (one writer, one read-only reader), derived-data cache, file privacy, card view."""

import asyncio
import os
import re
import sqlite3
import stat
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.db.repository import DatabaseRepository
from penguwatch.main import app
from penguwatch.models import Show, ShowStatus
from tests.test_api import setup_test_app  # noqa: F401  (fixture)


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "writes.db"))
    await r.initialize([])
    yield r
    await r.close()


class Boom(Exception):
    pass


@pytest.mark.asyncio
async def test_failed_write_rolls_back_without_touching_a_concurrent_write(repo, tmp_path):
    """A's transaction fails halfway while B (a different request) writes: B's work survives, A leaves nothing."""
    a_started, a_may_continue = asyncio.Event(), asyncio.Event()

    async def writer_a():
        async with repo.write() as db:
            await db.execute("INSERT INTO app_state (key, value) VALUES ('a1', 'x')")
            a_started.set()
            await a_may_continue.wait()  # B tries to write (and commit) right here
            await db.execute("INSERT INTO app_state (key, value) VALUES ('a2', 'x')")
            raise Boom()

    task_a = asyncio.create_task(writer_a())
    await a_started.wait()
    task_b = asyncio.create_task(repo.set_state("b", "kept"))
    await asyncio.sleep(0.05)
    assert not task_b.done(), "B must wait for A's transaction instead of committing it"
    # an independent connection sees nothing of A (nothing was committed) and nothing of B yet
    other = sqlite3.connect(tmp_path / "writes.db")
    assert other.execute("SELECT key FROM app_state WHERE key IN ('a1', 'a2', 'b')").fetchall() == []
    a_may_continue.set()
    with pytest.raises(Boom):
        await task_a
    await task_b
    assert await repo.get_state("a1") is None and await repo.get_state("a2") is None
    assert await repo.get_state("b") == "kept"
    assert other.execute("SELECT key FROM app_state WHERE key IN ('a1', 'a2')").fetchall() == []
    assert other.execute("SELECT value FROM app_state WHERE key = 'b'").fetchone() == ("kept",)
    other.close()


@pytest.mark.asyncio
async def test_interleaved_writers_each_commit_all_their_statements(repo):
    async def multi(prefix: str):
        async with repo.write() as db:
            for i in range(3):
                await db.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (f"{prefix}{i}", prefix))
                await asyncio.sleep(0)  # yield to the other coroutine mid-transaction

    await asyncio.gather(multi("a"), multi("b"), multi("c"))
    for p in "abc":
        assert [await repo.get_state(f"{p}{i}") for i in range(3)] == [p] * 3


@pytest.mark.asyncio
async def test_a_failing_write_is_rolled_back_not_left_pending(repo):
    """A write failing inside its transaction must not leak its first statement into the next commit."""
    with pytest.raises(sqlite3.OperationalError):
        async with repo.write() as db:
            await db.execute("INSERT INTO app_state (key, value) VALUES ('half', 'done')")
            await db.execute("INSERT INTO no_such_table VALUES (1)")
    await repo.set_state("later", "ok")  # commits whatever is pending on the connection
    assert await repo.get_state("half") is None and await repo.get_state("later") == "ok"


def test_no_module_commits_outside_the_write_context():
    src = Path(__file__).resolve().parent.parent / "src" / "penguwatch"
    allowed = {src / "db" / "repository.py", src / "api" / "exports.py"}  # exports: a private sqlite3 copy
    offenders = []
    for path in src.rglob("*.py"):
        if path in allowed or "migrations" in path.parts:
            continue
        if re.search(r"\.commit\(\)", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(src)))
    assert offenders == [], f"writes must use `async with repo.write()`: {offenders}"


@pytest.mark.asyncio
async def test_derived_value_is_reused_until_a_write_or_rollback(repo):
    calls = []

    async def build():
        calls.append(1)
        return len(calls)

    assert await repo.derived("k", build) == 1
    assert await repo.derived("k", build) == 1
    await repo.set_state("x", "1")
    assert await repo.derived("k", build) == 2
    with pytest.raises(Boom):
        async with repo.write():
            raise Boom()
    assert await repo.derived("k", build) == 3


@pytest.mark.asyncio
async def test_taste_profile_follows_library_changes(repo):
    first = await repo.taste_profile()
    assert await repo.taste_profile() is first
    await repo.upsert_show(Show(title="Space Show", genres=["Sci-Fi"], status=ShowStatus.COMPLETED, user_rating=9))
    second = await repo.taste_profile()
    assert second is not first and "Sci-Fi" in second.genre_count


@pytest.mark.asyncio
async def test_database_files_are_private_and_synchronous_normal(tmp_path):
    path = tmp_path / "priv" / "wt.db"
    r = DatabaseRepository(str(path))
    old = os.umask(0o022)  # a permissive umask: the repository must still tighten the files
    try:
        await r.initialize([])
        await r.set_state("k", "v")
        r._harden_file_modes()
        db = await r.get_connection()
        assert (await (await db.execute("PRAGMA synchronous")).fetchone())[0] == 1  # NORMAL
        assert (await (await db.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
        found = [p for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists()]
        assert path in found
        for p in found:
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    finally:
        os.umask(old)
        await r.close()


@pytest.mark.asyncio
async def test_startup_sets_a_private_umask_and_private_files(tmp_path, monkeypatch):
    for var in ("SIMKL_ACCESS_TOKEN", "SIMKL_CLIENT_ID", "TMDB_API_KEY", "PENGUWATCH_NOTIFY_URL"):
        monkeypatch.delenv(var, raising=False)
    db_path = tmp_path / "data" / "app.db"
    monkeypatch.setenv("PENGUWATCH_DB_PATH", str(db_path))
    old = os.umask(0o022)
    try:
        async with app.router.lifespan_context(app):
            current = os.umask(0o077)
            os.umask(current)
            assert current == 0o077
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    finally:
        os.umask(old)
        if hasattr(app.state, "container"):
            del app.state.container


@pytest.mark.asyncio
async def test_backup_does_not_use_the_shared_connection(setup_test_app, tmp_path):
    """The snapshot runs on its own connection: it works while a write transaction is open on the shared one."""
    repo = setup_test_app
    await repo.upsert_show(Show(title="Snap Me", status=ShowStatus.WATCHING))
    release = asyncio.Event()
    holding = asyncio.Event()

    async def hold_a_write_open():
        async with repo.write() as db:
            await db.execute("INSERT INTO app_state (key, value) VALUES ('pending', 'x')")
            holding.set()
            await release.wait()

    task = asyncio.create_task(hold_a_write_open())
    await holding.wait()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await asyncio.wait_for(ac.get("/api/export/backup.db"), timeout=10)
        assert res.status_code == 200
        out = tmp_path / "b.db"
        out.write_bytes(res.content)
        conn = sqlite3.connect(out)
        assert conn.execute("SELECT count(*) FROM shows WHERE title = 'Snap Me'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM app_state WHERE key = 'pending'").fetchone()[0] == 0  # uncommitted
        conn.close()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_card_view_is_slim_and_default_is_unchanged(setup_test_app):
    from penguwatch.api.shows import CARD_FIELDS

    repo = setup_test_app
    await repo.upsert_show(Show(title="Big Overview", overview="x" * 5000, status=ShowStatus.WATCHING,
                                genres=["Drama"], aired_episodes=10, watched_episodes=3, user_rating=8))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        full = (await ac.get("/api/shows")).json()
        card = (await ac.get("/api/shows?view=card")).json()
        interest = (await ac.get("/api/shows?view=card&sort_by=interest")).json()
        bad = await ac.get("/api/shows?view=nope")
    assert len(card) == len(full) == 1
    assert "overview" in full[0] and "created_at" in full[0]  # default shape unchanged
    assert set(card[0]) == CARD_FIELDS - {"interest_score", "interest_reasons"}
    assert "overview" not in card[0] and "banner" not in card[0] and "sequels" not in card[0]
    assert set(interest[0]) == CARD_FIELDS  # the score and reasons are present when ranking by interest
    for key in card[0]:
        assert card[0][key] == full[0][key]  # same values as the full view
    assert card[0]["episodes_left"] == 7 and card[0]["is_ready_to_binge"] is True
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_tidy_does_not_query_episodes_per_show(repo):
    from penguwatch.api.upnext import get_tidy

    for i in range(25):
        sid = await repo.upsert_show(Show(title=f"Done {i}", status=ShowStatus.COMPLETED, ids={"simkl": 1000 + i}))
        await repo.upsert_episode_list(sid, [{"season": 1, "number": n, "air_date": "2020-01-01"} for n in (1, 2)])
        await repo.set_episodes_watched(sid, [(1, 1)], True)
    statements = []
    db = await repo.read_connection()  # reads go through the read-only connection
    await db.set_trace_callback(statements.append)
    try:
        result = await get_tidy(months=6, repo=repo)
    finally:
        await db.set_trace_callback(None)
    assert len(result["returning"]) == 25
    episode_queries = [s for s in statements if "FROM episodes" in s]
    assert 1 <= len(episode_queries) <= 2, f"{len(episode_queries)} episode queries"


@pytest.mark.asyncio
async def test_slim_taste_profile_equals_the_one_built_from_full_shows(repo):
    from penguwatch.interest import build_profile
    from penguwatch.models import Priority

    specs = [
        ("A", ["Drama", "Crime"], ShowStatus.COMPLETED, None, 0, Priority.HIGH),
        ("B", ["Drama"], ShowStatus.WATCHING, None, 4, Priority.MEDIUM),
        ("C", ["Comedy", "Drama", "Drama"], ShowStatus.PLANTOWATCH, 9.0, 0, Priority.LOW),
        ("D", [], ShowStatus.DROPPED, None, 2, Priority.MEDIUM),
        ("E", ["Crime"], ShowStatus.WATCHING, 6.0, 0, Priority.HIGH),
        ("F", ["Comedy"], ShowStatus.HOLD, None, 3, Priority.MEDIUM),
    ]
    for title, genres, status, mine, watched, prio in specs:
        await repo.upsert_show(Show(title=title, genres=genres, status=status, user_rating=mine,
                                    watched_episodes=watched, priority=prio))
    assert await repo.taste_profile() == build_profile(await repo.get_shows())


@pytest.mark.asyncio
async def test_reads_do_not_see_a_concurrent_uncommitted_write(repo):
    """get_state used to run on the writer's connection and return another task's half-done transaction."""
    inside, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with repo.write() as db:
            await db.execute("INSERT INTO app_state (key, value) VALUES ('pending', 'x')")
            inside.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await inside.wait()
    assert await repo.get_state("pending") is None
    release.set()
    await task
    assert await repo.get_state("pending") == "x"


@pytest.mark.asyncio
async def test_reads_use_a_read_only_connection_and_writes_the_writer(repo):
    reader = await repo.read_connection()
    with pytest.raises(sqlite3.OperationalError):
        await reader.execute("INSERT INTO app_state (key, value) VALUES ('k', 'v')")
    assert (await (await reader.execute("PRAGMA foreign_keys")).fetchone())[0] == 0  # only the writer needs it
    writer = await repo.get_connection()
    assert (await (await writer.execute("PRAGMA foreign_keys")).fetchone())[0] == 1
    assert (await (await reader.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
    assert (await (await reader.execute("PRAGMA synchronous")).fetchone())[0] == 1
