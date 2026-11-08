"""History at library scale: 5,000 titles with ~3,000 watched episodes (about the owner's library) and with ~37,000
(ten times as many), over two years. Printed with -s; the only assertion is a generous ceiling that catches a query
going quadratic. "cold" is the first request after a write, "warm" a repeat (served from the cache)."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from penguwatch import clock
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("watched_every", [1, 12], ids=["heavy", "typical"])  # every Nth title has watched episodes
@pytest.mark.asyncio
async def test_history_timing_at_5k_titles(tmp_path, monkeypatch, watched_every):
    clock.configure("America/New_York")
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)
    repo = DatabaseRepository(str(tmp_path / "perf.db"))
    await repo.initialize([ServiceConfig(id=f"svc{i}", name=f"Service {i}", monthly_cost=9.99, is_active=i < 5) for i in range(8)])
    try:
        db = await repo.get_connection()
        genres = [["Drama", "Mystery"], ["Comedy"], ["Sci-Fi", "Thriller"], ["Anime", "Action"], []]
        await db.executemany(
            "INSERT INTO shows (id, title, status, media_type, service_id, runtime_minutes, genres_json, last_watched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(i, f"Title {i}", "completed" if i % 3 == 0 else "watching", "movie" if i % 10 == 0 else "show",
              f"svc{i % 8}", 20 + i % 50, json.dumps(genres[i % 5]),
              (NOW - timedelta(days=i % 700)).strftime("%Y-%m-%dT%H:%M:%SZ")) for i in range(1, 5001)])
        rows = []
        for i in range(1, 5001):
            if i % 10 == 0 or i % watched_every:
                continue  # movies have no episode rows
            for n in range(1, 1 + 1 + i % 15):
                at = NOW - timedelta(days=(i * 7 + n * 3) % 730, minutes=(i * 31 + n * 17) % 1440)
                stamp = (at.strftime("%Y-%m-%dT%H:%M:%SZ"), at.isoformat(), at.date().isoformat(), at.strftime("%Y-%m-%dT%H:%M:%SZ"))[(i + n) % 4]
                rows.append((i, 1, n, 1, stamp))
        await db.executemany("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, ?, ?, ?, ?)", rows)
        await db.commit()

        results = {}
        for days in (7, 30, 365):
            await repo.set_state("perf", str(days))  # a write: the next call is cold
            t0 = time.perf_counter()
            data = await repo.get_history(days)
            cold = (time.perf_counter() - t0) * 1000
            warm = []
            for _ in range(5):
                t0 = time.perf_counter()
                await repo.get_history(days)
                warm.append((time.perf_counter() - t0) * 1000)
            results[days] = (cold, min(warm), data["totals"]["current"]["episodes"])
            assert cold < 1000, (days, cold)
        print("\n[perf] /api/history, 5k titles, %d watched episodes: " % len(rows)
              + "; ".join(f"{d} days: cold {c:.1f} ms, warm {w:.2f} ms ({n} episodes)" for d, (c, w, n) in results.items()))
    finally:
        await repo.close()
        clock.configure(None)
