"""The sync review names titles the way the rest of the page does: by the English name when the title has one."""

import json

import pytest

from penguwatch.api.sync_review import get_sync_review
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.simkl.client import SimklClient
from penguwatch.models import MediaType, Show, ShowStatus
from tests.simkl_helpers import simkl_engine


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "titles.db"), key_providers=("simkl",))
    await repo.initialize([])
    await repo.set_state("sync_review_mode", "on")
    client = SimklClient(client_id="cid", access_token="tok")
    yield repo, simkl_engine(client, repo)
    await client.aclose()
    await repo.close()


async def test_outgoing_and_incoming_rows_carry_the_english_name(env):
    repo, sync = env
    sid = await repo.upsert_show(Show(title="Tempal: Item no Chikara", en_title="Overgeared", ids={"simkl": 77},
                                      media_type=MediaType.ANIME, status=ShowStatus.WATCHING))
    await sync.update_show_status(sid, ShowStatus.HOLD)  # an outgoing row, waiting for approval
    # an incoming entry staged before entries recorded the English name (as on an upgraded install)
    staged = {"at": "2026-11-06T00:00:00+00:00", "entries": [
        {"key": "anime:77", "kind": "update", "media": "anime", "ids": {"simkl": 77}, "show_id": sid,
         "title": "Tempal: Item no Chikara", "poster": None, "changes": [{"field": "status", "local": "hold",
                                                                          "remote": "watching"}]},
        {"key": "anime:78", "kind": "new", "media": "anime", "ids": {"simkl": 78}, "show_id": None,
         "title": "Kimi no Na wa", "en_title": "Your Name", "poster": None, "changes": []},
    ]}
    await repo.set_state("incoming_staged", json.dumps(staged))
    review = await get_sync_review(repo, sync)
    [out] = review["outgoing"]
    assert (out["title"], out["en_title"]) == ("Tempal: Item no Chikara", "Overgeared")
    names = {e["key"]: (e["title"], e.get("en_title")) for e in review["incoming"]}
    assert names == {"anime:77": ("Tempal: Item no Chikara", "Overgeared"), "anime:78": ("Kimi no Na wa", "Your Name")}
