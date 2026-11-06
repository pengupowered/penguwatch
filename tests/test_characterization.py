"""Characterization against the sanitized seed library (scripts/test.sh --seed).

The driver layer changed the API shape (titles carry `ids` instead of simkl_id/imdb_id/tmdb_id/tvdb_id), so the
pre-driver golden (.seed/golden.json, never rewritten) is mapped to the new shape mechanically
(characterization.map_pre_driver_golden) and must then equal today's answers, apart from the differences
characterization.EXPLAINED lists with their reasons. .seed/golden.drivers.json is today's golden, recorded with
`python -m tests.characterization record /seed/penguwatch.db /seed/golden.drivers.json`."""

import os
from pathlib import Path

import pytest

from tests import characterization

SEED = os.environ.get("PENGUWATCH_SEED_DIR")
needs_seed = pytest.mark.skipif(not SEED, reason="needs the sanitized seed library (scripts/test.sh --seed)")


@needs_seed
def test_api_snapshot_matches_the_pre_driver_golden_mapped_to_the_new_shape():
    assert characterization.compare_mapped(f"{SEED}/penguwatch.db", f"{SEED}/golden.json") == []


@needs_seed
def test_api_snapshot_matches_the_driver_golden():
    golden = Path(f"{SEED}/golden.drivers.json")
    if not golden.exists():
        pytest.skip("golden.drivers.json not recorded yet")
    assert characterization.compare(f"{SEED}/penguwatch.db", str(golden)) == []


def test_the_mapping_turns_id_fields_into_ids_everywhere():
    old = {"/api/shows": {"status": 200, "body": {"json": [
        {"id": 1, "imdb_id": "tt1", "simkl_id": 5, "tmdb_id": None, "tvdb_id": 7, "title": "A",
         "sequels": [{"simkl_id": 9, "title": "A2"}]}]}},
        "/api/export/library.csv": {"status": 200, "body": {"text": "simkl_id\n5\n"}}}
    mapped = characterization.map_pre_driver_golden(old)
    assert mapped["/api/shows"]["body"]["json"] == [
        {"id": 1, "ids": {"imdb": "tt1", "simkl": 5, "tvdb": 7}, "sequels": [{"ids": {"simkl": 9}, "title": "A2"}],
         "title": "A"}]
    assert mapped["/api/export/library.csv"] == old["/api/export/library.csv"]  # text bodies stay as they were
