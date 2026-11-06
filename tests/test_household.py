"""Configurable household: names from config.yaml, neutral stored tags, and the page with one or two people."""

import html
import json
import re

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from penguwatch.config import AppConfig, HouseholdConfig, load_config
from penguwatch.db.migrations import MIGRATIONS
from penguwatch.main import app


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


def _meta_names(page: str) -> dict:
    m = re.search(r'<meta name="penguwatch-household" content="([^"]*)">', page)
    assert m, "household meta tag missing"
    return json.loads(html.unescape(m.group(1)))


def _options(page: str, select_id: str) -> list:
    m = re.search(rf'<select id="{select_id}"[^>]*>(.*?)</select>', page, re.S)
    assert m, f"{select_id} missing"
    return re.findall(r'<option value="([^"]*)"', m.group(1))


def test_default_household_is_two_neutral_names():
    hh = AppConfig().household
    assert hh.members == ["Me", "Partner"] and hh.shared
    assert hh.labels() == {"both": "Both", "person1": "Me", "person2": "Partner"}


def test_one_member_has_no_both():
    hh = HouseholdConfig(members=["  Sam "])
    assert not hh.shared and hh.labels() == {"person1": "Sam"}


@pytest.mark.parametrize("members", [[], ["A", "B", "C"], ["A", "  "], ["x" * 41]])
def test_invalid_members_are_refused(members):
    with pytest.raises(ValidationError):
        HouseholdConfig(members=members)


def test_household_is_read_from_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('household:\n  members: ["Alex", "Sam"]\n', encoding="utf-8")
    assert load_config(str(cfg)).household.members == ["Alex", "Sam"]


async def test_migration_turns_unknown_tags_into_person1(tmp_path):
    db = tmp_path / "old.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY, household_tag TEXT)")
        await conn.executemany("INSERT INTO shows (household_tag) VALUES (?)",
                               [("person1",), ("person2",), ("both",), ("legacy",), (None,)])
        migrate = dict((v, fn) for v, _, fn in MIGRATIONS)[10]
        await migrate(conn)
        rows = [r[0] for r in await (await conn.execute("SELECT household_tag FROM shows ORDER BY id")).fetchall()]
    assert rows == ["person1", "person2", "both", "person1", "person1"]


async def test_page_shows_the_configured_names(container):
    container.config.household = HouseholdConfig(members=["Alex", "<Sam>"])
    page = await _index()
    assert _meta_names(page) == {"both": "Both", "person1": "Alex", "person2": "<Sam>"}
    assert _options(page, "filter-household-select") == ["", "both", "person1", "person2"]
    assert _options(page, "decide-audience") == ["both", "person1", "person2", "all"]
    assert "&lt;Sam&gt;" in page and "<Sam>" not in page  # names are escaped


async def test_page_for_one_person_hides_the_audience_controls(container):
    container.config.household = HouseholdConfig(members=["Alex"])
    page = await _index()
    assert _meta_names(page) == {"person1": "Alex"}
    assert 'id="filter-household-select"' not in page
    assert _options(page, "decide-audience") == ["all"]
    assert _options(page, "m-household") == ["person1"]


async def test_api_takes_neutral_tags_only(container):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        created = await ac.post("/api/shows", json={"title": "Tagged", "household_tag": "person2"})
        assert created.status_code == 200, created.text
        assert created.json()["household_tag"] == "person2"
        unknown = await ac.post("/api/shows", json={"title": "Old", "household_tag": "someone"})
        assert unknown.status_code == 422
