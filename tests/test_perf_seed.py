"""Timing of hot endpoints against the sanitized seed library: printed, never asserted (run with -s)."""

import os
import shutil
import time

import pytest

from penguwatch.api.upnext import get_tidy
from penguwatch.db.repository import DatabaseRepository

SEED = os.environ.get("PENGUWATCH_SEED_DIR")


@pytest.mark.skipif(not SEED, reason="needs the sanitized seed library (scripts/test.sh --seed)")
@pytest.mark.asyncio
async def test_tidy_timing_on_seed(tmp_path):
    db_file = str(tmp_path / "seed.db")
    shutil.copy(f"{SEED}/penguwatch.db", db_file)
    repo = DatabaseRepository(db_file)
    await repo.initialize()
    try:
        await get_tidy(months=6, repo=repo)  # warm-up (imports, page cache)
        runs, cold = [], []
        for _ in range(5):
            await repo.set_state("perf", "x")  # a write drops the cached taste profile: the next call is cold
            t0 = time.perf_counter()
            await get_tidy(months=6, repo=repo)
            cold.append((time.perf_counter() - t0) * 1000)
        for _ in range(5):
            t0 = time.perf_counter()
            result = await get_tidy(months=6, repo=repo)
            runs.append((time.perf_counter() - t0) * 1000)
        print(f"\n[perf] /api/tidy on seed: after a write (cold profile) ms = {[round(r, 1) for r in cold]}; "
              f"warm ms = {[round(r, 1) for r in runs]}; "
              f"returning={len(result['returning'])} stale={len(result['stale'])} unassigned={len(result['unassigned'])}")
    finally:
        await repo.close()
