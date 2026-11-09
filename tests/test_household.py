"""A household of 1 to N people: the members (ids and names), the page with one, two and three people, who wants a title
through the API, and the scoring with more than two people. The migration from the one-value tag is in
tests/test_migrations.py, the settings saves in tests/test_household_settings.py, and "two people score as before" in
tests/test_household_golden.py."""

import html
import json
import re

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from penguwatch import pick
from penguwatch.api.discover import recommendation_seeds
from penguwatch.config import AppConfig, HouseholdConfig, load_config, normal_members
from penguwatch.db.migrations import MIGRATIONS
from penguwatch.interest import build_profile, score
from penguwatch.main import app
from penguwatch.models import Priority, Show, ShowStatus
from penguwatch.optimizer.plannable import plannable_hours

P1, P2, P3 = "person1", "person2", "person3"


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


def _meta_members(page: str) -> list:
    m = re.search(r'<meta name="penguwatch-household" content="([^"]*)">', page)
    assert m, "household meta tag missing"
    return json.loads(html.unescape(m.group(1)))


def _options(page: str, select_id: str) -> list:
    m = re.search(rf'<select id="{select_id}"[^>]*>(.*?)</select>', page, re.S)
    assert m, f"{select_id} missing"
    return re.findall(r'<option value="([^"]*)"', m.group(1))


def _toggles(page: str, name: str) -> list:
    return re.findall(rf'<input type="checkbox" name="{name}" value="([^"]*)"', page)


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- the members -------------------------------------------------------------------------------------------------------

def test_default_household_is_two_people_with_stable_ids():
    hh = AppConfig().household
    assert hh.page_members() == [{"id": P1, "name": "Me"}, {"id": P2, "name": "Partner"}]
    assert hh.shared and hh.ids == [P1, P2] and hh.first == P1


def test_the_old_shape_reads_as_person1_person2_in_order():
    hh = HouseholdConfig(members=["  Sam ", "Alex", "Kim"])
    assert hh.page_members() == [{"id": P1, "name": "Sam"}, {"id": P2, "name": "Alex"}, {"id": P3, "name": "Kim"}]


def test_one_member_is_not_shared():
    hh = HouseholdConfig(members=["Sam"])
    assert not hh.shared and hh.ids == [P1]


def test_twenty_members_are_allowed_and_ids_follow_the_given_ones():
    hh = HouseholdConfig(members=[{"id": "person7", "name": "A"}, *({"name": f"N{i}"} for i in range(19))])
    assert len(hh.members) == 20 and hh.ids[:3] == ["person7", "person8", "person9"]


def test_a_new_member_gets_an_id_above_the_floor():
    assert normal_members([{"id": P1, "name": "A"}, {"name": "B"}], floor=5) == [
        {"id": P1, "name": "A"}, {"id": "person6", "name": "B"}]


@pytest.mark.parametrize("members", [
    [], [f"N{i}" for i in range(21)], ["A", "  "], ["x" * 41], ["Ann", "ann"],
    [{"id": P1, "name": "A"}, {"id": P1, "name": "B"}], [{"id": "partner", "name": "A"}],
])
def test_invalid_members_are_refused(members):
    with pytest.raises(ValidationError):
        HouseholdConfig(members=members)


def test_names_of_and_current_keep_the_members_order_and_leave_out_others():
    hh = HouseholdConfig(members=["Ann", "Bob", "Cid"])
    assert hh.current([P3, "person9", P1]) == [P1, P3]
    assert hh.names_of([P3, "person9", P1]) == ["Ann", "Cid"]


def test_household_is_read_from_yaml_in_either_shape(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('household:\n  members: ["Alex", "Sam"]\n', encoding="utf-8")
    assert load_config(str(cfg)).household.page_members() == [{"id": P1, "name": "Alex"}, {"id": P2, "name": "Sam"}]
    cfg.write_text('household:\n  members: [{id: person1, name: Alex}, {id: person4, name: Sam}]\n', encoding="utf-8")
    assert load_config(str(cfg)).household.ids == [P1, "person4"]


async def test_migration_0010_turns_unknown_tags_into_person1(tmp_path):
    db = tmp_path / "old.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY, household_tag TEXT)")
        await conn.executemany("INSERT INTO shows (household_tag) VALUES (?)",
                               [("person1",), ("person2",), ("both",), ("legacy",), (None,)])
        migrate = dict((v, fn) for v, _, fn in MIGRATIONS)[10]
        await migrate(conn)
        rows = [r[0] for r in await (await conn.execute("SELECT household_tag FROM shows ORDER BY id")).fetchall()]
    assert rows == ["person1", "person2", "both", "person1", "person1"]


# --- the page ----------------------------------------------------------------------------------------------------------

async def test_page_with_two_people_keeps_the_both_wording(container):
    container.config.household = HouseholdConfig(members=["Alex", "<Sam>"])
    page = await _index()
    assert _meta_members(page) == [{"id": P1, "name": "Alex"}, {"id": P2, "name": "<Sam>"}]
    assert _options(page, "filter-household-select") == ["", P1, P2, "shared"]
    assert ">Alex only</option>" in page
    assert _options(page, "decide-audience") == ["shared", P1, P2, "all"]
    assert "Both want to watch</option>" in page and ">Both want</option>" in page
    assert _toggles(page, "m-household") == [P1, P2]
    assert "&lt;Sam&gt;" in page and "<Sam>" not in page  # names are escaped


async def test_page_with_three_people_has_a_choice_each_and_shared(container):
    container.config.household = HouseholdConfig(members=["Ann", "Bob", "Cid"])
    page = await _index()
    assert [m["id"] for m in _meta_members(page)] == [P1, P2, P3]
    assert _options(page, "filter-household-select") == ["", P1, P2, P3, "shared"]
    assert _options(page, "decide-audience") == ["shared", P1, P2, P3, "all"]
    assert "Cid's pick" in page and ">Shared</option>" in page and "Both want" not in page
    assert _toggles(page, "m-household") == [P1, P2, P3]


async def test_page_for_one_person_hides_the_audience_controls(container):
    container.config.household = HouseholdConfig(members=["Alex"])
    page = await _index()
    assert _meta_members(page) == [{"id": P1, "name": "Alex"}]
    assert 'id="filter-household-select"' not in page
    assert _options(page, "decide-audience") == ["all"]
    assert re.search(r'<div class="field field-who" hidden>', page)
    assert re.search(r'<div class="field"\s+hidden>\s*<label for="decide-audience"', page)


# --- the API -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def three(container):
    container.config.household = HouseholdConfig(members=["Ann", "Bob", "Cid"])
    container.repository.set_household_members([P1, P2, P3])
    return container


async def test_a_title_is_for_the_people_it_names_or_the_first(three):
    async with client() as ac:
        named = await ac.post("/api/shows", json={"title": "Named", "household": [P3, P1]})
        nobody = await ac.post("/api/shows", json={"title": "Nobody"})
        unknown = await ac.post("/api/shows", json={"title": "Old", "household": ["person9"]})
        old_field = await ac.post("/api/shows", json={"title": "Older", "household_tag": "both"})
    assert named.status_code == 200 and named.json()["household"] == [P1, P3]  # the members' order
    assert nobody.json()["household"] == [P1]
    assert unknown.status_code == 422 and "person9" in unknown.text
    assert old_field.status_code == 200 and old_field.json()["household"] == [P1]  # the old field is not read
    assert "household_tag" not in named.json()


async def test_patch_replaces_the_set_and_refuses_nobody_or_strangers(three):
    async with client() as ac:
        sid = (await ac.post("/api/shows", json={"title": "T"})).json()["id"]
        both = await ac.patch(f"/api/shows/{sid}", json={"household": [P2, P3]})
        empty = await ac.patch(f"/api/shows/{sid}", json={"household": []})
        stranger = await ac.patch(f"/api/shows/{sid}", json={"household": [P1, "person8"]})
        kept = await ac.patch(f"/api/shows/{sid}", json={"priority": "high"})
    assert both.json()["household"] == [P2, P3]
    assert empty.status_code == 422 and stranger.status_code == 422
    assert kept.json()["household"] == [P2, P3]


async def test_list_filters_by_member_and_shared(three):
    async with client() as ac:
        for title, who in (("Ann's", [P1]), ("Bob's", [P2]), ("Ann+Cid", [P1, P3]), ("All", [P1, P2, P3])):
            assert (await ac.post("/api/shows", json={"title": title, "household": who})).status_code == 200

        async def titles(**params):
            res = await ac.get("/api/shows", params=params)
            assert res.status_code == 200, res.text
            return sorted(s["title"] for s in res.json())

        assert await titles(member=P1) == ["All", "Ann's", "Ann+Cid"]
        assert await titles(member=[P2, P3]) == ["All", "Ann+Cid", "Bob's"]
        assert await titles(shared="true") == ["All", "Ann+Cid"]
        assert await titles(shared="true", member=P2) == ["All"]
        assert await titles(shared="false") == ["Ann's", "Bob's"]  # one person alone
        assert await titles(shared="false", member=P1) == ["Ann's"]  # the page's "Ann only"
        cards = (await ac.get("/api/shows", params={"view": "card", "member": P3})).json()
        assert {c["title"]: c["household"] for c in cards} == {"Ann+Cid": [P1, P3], "All": [P1, P2, P3]}
        assert (await ac.get("/api/shows", params={"member": "person9"})).status_code == 422


async def test_shared_filter_counts_only_current_members(three):
    """A stored id of someone no longer in the household is ignored: a title for Ann and a former member is not
    shared, and shows Ann only."""
    repo = three.repository
    async with client() as ac:
        sid = (await ac.post("/api/shows", json={"title": "Half", "household": [P1]})).json()["id"]
    async with repo.write() as db:
        await db.execute("INSERT INTO show_household (show_id, member_id) VALUES (?, 'person9')", (sid,))
    async with client() as ac:
        assert (await ac.get("/api/shows", params={"shared": "true"})).json() == []
        assert (await ac.get(f"/api/shows/{sid}")).json()["household"] == [P1]


async def test_decide_takes_member_or_shared(three):
    async with client() as ac:
        for title, who in (("Solo", [P2]), ("Pair", [P1, P3])):
            await ac.post("/api/shows", json={"title": title, "household": who, "status": "plantowatch"})
        base = {"only_active_services": False}
        shared = (await ac.post("/api/decide/pick", json={**base, "shared": True})).json()
        mine = (await ac.post("/api/decide/pick", json={**base, "member": P2})).json()
        own = (await ac.post("/api/decide/pick", json={**base, "member": P3, "shared": False})).json()
        any_of_cid = (await ac.post("/api/decide/pick", json={**base, "member": P3})).json()
        stranger = await ac.post("/api/decide/pick", json={**base, "member": "person9"})
    assert shared["result"]["title"] == "Pair" and shared["total_matching"] == 1
    assert mine["result"]["title"] == "Solo"
    assert own["result"] is None and any_of_cid["result"]["title"] == "Pair"
    assert stranger.status_code == 422


async def test_the_csv_says_who_by_name(three):
    async with client() as ac:
        await ac.post("/api/shows", json={"title": "Pair", "household": [P3, P1]})
        text = (await ac.get("/api/export/library.csv")).text
    header, row = text.splitlines()[:2]
    columns = header.split(",")
    assert "who" in columns and "audience" not in columns
    assert row.split(",")[columns.index("who")] == "Ann; Cid"


async def test_a_new_season_is_for_its_parents_people(three, monkeypatch):
    from penguwatch.api import upnext
    seen = {}

    async def fake_create(payload, repo, sync, background):
        seen["household"] = payload.household
        return None

    monkeypatch.setattr(upnext, "_create_title", fake_create)
    async with client() as ac:
        parent = (await ac.post("/api/shows", json={"title": "S1", "household": [P2, P3]})).json()["id"]
        res = await ac.post("/api/tidy/apply", json={"status": "plantowatch",
                                                  "sequels": [{"parent_id": parent, "title": "S2", "ids": {"simkl": 5}}]})
    assert res.status_code == 200, res.text
    assert seen["household"] == [P2, P3]


# --- scoring with three people ------------------------------------------------------------------------------------------

def _show(who, **kw):
    return Show(**{"title": "T", "household": who, "imdb_rating": 8.0, "imdb_votes": 10000, "genres": ["Drama"], **kw})


@pytest.mark.parametrize("who, size, reason", [
    ([P1], 3, None), ([P1, P2], 2, "Both want it"), ([P1, P2], 3, "Shared by 2"), ([P1, P2, P3], 3, "Everyone wants it"),
    ([P1, P2, P3], 4, "Shared by 3"),
])
def test_the_shared_reason_says_how_many(who, size, reason):
    _, reasons = score(_show(who), build_profile([]), size)
    assert (reason in reasons) if reason else not any("want" in r or "Shared" in r for r in reasons)


def test_interest_grows_per_extra_person_and_is_clamped():
    profile = build_profile([])
    one, two, three_ = (score(_show(w, priority=Priority.LOW), profile, 3)[0] for w in ([P1], [P1, P2], [P1, P2, P3]))
    assert one < two < three_
    high = [score(_show(w, priority=Priority.HIGH), profile, 3)[0] for w in ([P1, P2], [P1, P2, P3])]
    assert high[0] == high[1]  # the household signal is clamped at 1 (0.5 for high priority + 0.5 per extra person)


def test_pick_weight_is_three_for_two_people_and_nine_from_three_on():
    weights = [pick.weight(_show(w, priority=Priority.LOW, imdb_rating=None)) for w in
               ([P1], [P1, P2], [P1, P2, P3], [P1, P2, P3, "person4"])]
    assert weights == [1.0, 3.0, 9.0, 9.0]


def test_a_shared_title_counts_in_full_for_the_plan():
    def hours(who):
        return plannable_hours(Show(title="T", household=who, status=ShowStatus.HOLD, aired_episodes=4,
                                    runtime_minutes=60))
    assert (hours([P1]), hours([P1, P3])) == (1.0, 4.0)


def test_discover_seeds_sort_by_how_many_people_want_them():
    shows = [Show(title=t, household=w, priority=Priority.HIGH, ids={"simkl": n + 1}, imdb_rating=r)
             for n, (t, w, r) in enumerate((("Two", [P1, P2], 9.0), ("Three", [P1, P2, P3], 7.0), ("One", [P1], 9.5)))]
    assert [s.title for s in recommendation_seeds(shows, "simkl")] == ["Three", "Two"]


async def test_the_optimizer_boost_grows_per_extra_person(three):
    from penguwatch.config import OptimizerConfig
    from penguwatch.models import ServiceConfig
    from penguwatch.optimizer.engine import SubscriptionOptimizer
    repo = three.repository
    await repo.upsert_service(ServiceConfig(id="svc", name="Svc", monthly_cost=5.0))
    plan = SubscriptionOptimizer(repository=repo, config=OptimizerConfig(household_priority_boost=1.0))

    async def svc_score():
        return next(r for r in (await plan.calculate_plan()).service_rankings if r.service_id == "svc")

    sid = await repo.upsert_show(Show(title="T", service_id="svc", household=[P1], status=ShowStatus.PLANTOWATCH))
    base = await svc_score()
    await repo.update_show_metadata(sid, household=[P1, P2, P3])
    shared = await svc_score()
    assert shared.shared_count == 1 and base.shared_count == 0
    # plannable hours are 0 (nothing aired), so the difference is the boost alone: 2 extra people x 1.0 x 10
    assert round(shared.binge_score - base.binge_score, 1) == 20.0
