"""The household's members on the Settings tab: ids are kept on a rename, given to new people above every id given
before (never reused), a removed person's titles lose them (a title left with nobody goes to the first person, and the
save warns how many first), and every place a value can come from accepts the old list of names."""

import json

import pytest

from penguwatch.settings import appliers
from penguwatch.settings.hub import SettingsHub
from penguwatch.settings.manager import SettingsInvalid
from penguwatch.models import Show
from tests.settings_helpers import attach_manager

P1, P2, P3 = "person1", "person2", "person3"


@pytest.fixture
async def store(container, tmp_path):
    hub = SettingsHub()
    appliers.register_core(hub, container.config, container.repository)
    manager = await attach_manager(container, tmp_path, {"household": {"members": ["Ann", "Bob"]}}, hub=hub)
    container.repository.set_household_members(container.config.household.ids)
    return manager


async def save(manager, members):
    return await manager.apply(manager.revision, {"household.members": members}, {})


async def title(repo, name, who):
    return await repo.upsert_show(Show(title=name, household=who))


async def people(repo, show_id):
    return (await repo.get_show_by_id(show_id)).household


async def stored_rows(repo):
    db = await repo.read_connection()
    return sorted(tuple(r) for r in await (await db.execute("SELECT show_id, member_id FROM show_household")).fetchall())


async def test_the_import_stores_ids_and_a_rename_keeps_them(store, container):
    repo = container.repository
    assert store.document.values["household.members"] == [{"id": P1, "name": "Ann"}, {"id": P2, "name": "Bob"}]
    bobs = await title(repo, "Bob's", [P2])
    applied = await save(store, [{"id": P1, "name": "Ann"}, {"id": P2, "name": "Robert"}])
    assert applied.reload and "Who watches Ann, Bob → Ann, Robert" in applied.summary
    assert await people(repo, bobs) == [P2]
    assert container.config.household.names_of([P2]) == ["Robert"]


async def test_a_new_person_gets_the_next_id_and_an_id_is_never_reused(store, container):
    repo = container.repository
    await save(store, [{"id": P1, "name": "Ann"}, {"id": P2, "name": "Bob"}, {"name": "Cid"}])
    assert container.config.household.ids == [P1, P2, P3]
    assert repo.household_members == (P1, P2, P3)  # the core applier
    cids = await title(repo, "Cid's", [P3])
    shared = await title(repo, "Shared", [P1, P3])
    await save(store, [{"id": P1, "name": "Ann"}, {"id": P2, "name": "Bob"}])  # Cid leaves
    assert await people(repo, cids) == [P1] and await people(repo, shared) == [P1]
    assert all(m != P3 for _, m in await stored_rows(repo))  # the removed person's rows are gone
    await save(store, [{"id": P1, "name": "Ann"}, {"id": P2, "name": "Bob"}, {"name": "Cid"}])  # a new Cid
    assert container.config.household.ids == [P1, P2, "person4"]
    assert await people(repo, cids) == [P1]  # nothing comes back with the name


async def test_an_id_that_is_not_a_member_now_is_treated_as_new(store, container):
    await save(store, [{"id": P1, "name": "Ann"}, {"id": "person9", "name": "Eve"}])
    assert container.config.household.ids == [P1, P3]  # Bob left (person2 is spent); Eve is new: person3


async def test_removing_a_person_warns_how_many_titles_move(store, container):
    repo = container.repository
    only_bob = await title(repo, "Only Bob", [P2])
    both = await title(repo, "Both", [P1, P2])
    await title(repo, "Ann's", [P1])
    check = await store.validate({"household.members": [{"id": P1, "name": "Ann"}]}, {})
    assert check.ok
    assert [w["message"] for w in check.warnings] == [
        "2 titles are for Bob; saving takes that person off them. 1 of them is for no one else and moves to Ann."]
    await save(store, [{"id": P1, "name": "Ann"}])
    assert await people(repo, only_bob) == [P1] and await people(repo, both) == [P1]
    assert repo.household_members == (P1,)


async def test_removing_the_first_person_moves_their_titles_to_the_new_first(store, container):
    repo = container.repository
    anns = await title(repo, "Ann's", [P1])
    check = await store.validate({"household.members": [{"id": P2, "name": "Bob"}]}, {})
    assert check.warnings[0]["message"] == "1 title is for Ann; saving takes that person off it. It is for no one else and moves to Bob."
    await save(store, [{"id": P2, "name": "Bob"}])
    assert await people(repo, anns) == [P2]


@pytest.mark.parametrize("members, why", [
    ([], "at least 1"), ([{"id": P1, "name": "Ann"}, {"name": "ann"}], "different name"),
    ([{"name": " "}], "1 to 40"), ([{"name": f"N{i}"} for i in range(21)], "at most 20"),
])
async def test_refused_members_are_placed_on_the_setting(store, members, why):
    with pytest.raises(SettingsInvalid) as refused:
        await save(store, members)
    assert any(e["where"] == "values.household.members" and why in e["message"].lower()
               for e in refused.value.errors), refused.value.errors


async def test_the_old_shape_saves_too(store, container):
    await save(store, ["Ann", "Bobby", "Cid"])
    assert container.config.household.page_members() == [
        {"id": P1, "name": "Ann"}, {"id": P2, "name": "Bobby"}, {"id": P3, "name": "Cid"}]


async def test_a_kept_version_of_the_old_shape_restores_as_ids(store, container):
    repo = container.repository
    async with repo.write() as db:  # revision 1 as a build before 0.17 kept it
        await db.execute("UPDATE settings_versions SET document = ? WHERE revision = 1",
                         (json.dumps({"values": {"household.members": ["Ann", "Bob"]}, "lists": {}}),))
    await save(store, [{"id": P1, "name": "Ann"}])
    draft = await store.version(1)
    assert draft["values"]["household.members"] == [{"id": P1, "name": "Ann"}, {"id": P2, "name": "Bob"}]
    await store.apply(store.revision, draft["values"], draft["lists"], source="restore")
    assert container.config.household.ids == [P1, P3]  # Bob comes back as someone new: person2 is never given again


async def test_an_export_carries_ids_and_an_old_file_imports_as_ids(store):
    assert "- id: person1\n    name: Ann\n  - id: person2\n    name: Bob" in store.export_yaml()
    assert store.import_yaml(store.export_yaml())["values"]["household.members"] == [
        {"id": P1, "name": "Ann"}, {"id": P2, "name": "Bob"}]
    old = store.import_yaml("household:\n  members: [Kim, Lee, Max]\n")
    assert old["values"]["household.members"] == [{"id": P1, "name": "Kim"}, {"id": P2, "name": "Lee"},
                                                  {"id": P3, "name": "Max"}]


async def test_a_stored_value_of_the_old_shape_still_loads(container, tmp_path):
    repo = container.repository
    manager = await attach_manager(container, tmp_path, {})
    async with repo.write() as db:  # a row the migration did not see (written by a build in between)
        await db.execute("INSERT OR REPLACE INTO settings_values (key, value, updated) VALUES "
                         "('household.members', '[\"Kim\", \"Lee\", \"Max\"]', 0)")
    await manager.load()
    assert manager.config.household.ids == [P1, P2, P3] and manager.refused_keys == set()
