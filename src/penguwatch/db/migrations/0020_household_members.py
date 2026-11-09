"""0020: a household of 1 to N people. Who wants a title is a set of member ids in `show_household` (one row per title
and person), instead of the one value `shows.household_tag` (person1, person2 or both).

In the migration's one transaction:
- `show_household(show_id, member_id)`, keyed by both, with an index on member_id; a title's rows go with it;
- every title gets its people: person1 -> {person1}, person2 -> {person2}, both -> {person1, person2}; an empty or
  unknown value -> {person1}, as 0010 did. When the stored household has one person (household.members holds one
  name), every title gets {person1}: nobody else is in the household to want it;
- the check that every title has at least one row (it raises, and the transaction rolls back, otherwise), then the
  index idx_shows_household and the column household_tag go (ALTER TABLE ... DROP COLUMN, as 0013 did);
- the stored setting household.members, when it is the old list of names, becomes [{"id": "person1", "name": ...},
  ...] in order (penguwatch.config.normal_members), and app_state `household.last_member` records the highest id given
  (nothing when the store holds no members: the default two are person1 and person2, and the settings save counts the
  current members' ids too).

A second run changes nothing: the column is gone, the rows are kept (INSERT OR IGNORE), and the setting is already in
its new shape. Kept settings versions keep the old shape: restoring one reads it the same way (config.normal_members).
"""

import json

import aiosqlite

LAST_MEMBER_KEY = "household.last_member"  # app_state: the highest N of person<N> ever given (never reused)


async def _stored_members(conn: aiosqlite.Connection):
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings_values'")
    if await cursor.fetchone() is None:
        return None
    row = await (await conn.execute(
        "SELECT value FROM settings_values WHERE key = 'household.members'")).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _new_shape(members):
    """The old list of names as [{"id", "name"}]; anything else as it is (the settings manager reads it leniently)."""
    if isinstance(members, list) and members and all(isinstance(m, str) for m in members):
        return [{"id": f"person{n}", "name": name.strip()} for n, name in enumerate(members, start=1)]
    return members


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS show_household ("
        "show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE, "
        "member_id TEXT NOT NULL, "
        "PRIMARY KEY (show_id, member_id))")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_show_household_member ON show_household(member_id)")

    stored = await _stored_members(conn)
    new_members = _new_shape(stored)
    solo = isinstance(new_members, list) and len(new_members) == 1

    cursor = await conn.execute("PRAGMA table_info(shows)")
    if "household_tag" in {row[1] for row in await cursor.fetchall()}:
        # person1: the person1 titles, the "both" titles, and every empty or unknown value (0010's rule); with one
        # person, every title
        await conn.execute(
            "INSERT OR IGNORE INTO show_household (show_id, member_id) "
            "SELECT id, 'person1' FROM shows "
            + ("" if solo else "WHERE household_tag IS NULL OR household_tag != 'person2'"))
        if not solo:
            await conn.execute(
                "INSERT OR IGNORE INTO show_household (show_id, member_id) "
                "SELECT id, 'person2' FROM shows WHERE household_tag IN ('person2', 'both')")
        missing = (await (await conn.execute(
            "SELECT COUNT(*) FROM shows s WHERE NOT EXISTS "
            "(SELECT 1 FROM show_household h WHERE h.show_id = s.id)")).fetchone())[0]
        if missing:
            raise RuntimeError(f"0020: {missing} title(s) would be left with nobody; the column was not dropped")
        await conn.execute("DROP INDEX IF EXISTS idx_shows_household")
        await conn.execute("ALTER TABLE shows DROP COLUMN household_tag")
    else:
        # a database made after this migration's table (or a second run): every title still has someone
        await conn.execute(
            "INSERT OR IGNORE INTO show_household (show_id, member_id) "
            "SELECT id, 'person1' FROM shows s WHERE NOT EXISTS "
            "(SELECT 1 FROM show_household h WHERE h.show_id = s.id)")

    if new_members is not stored and new_members != stored:
        await conn.execute("UPDATE settings_values SET value = ? WHERE key = 'household.members'",
                           (json.dumps(new_members, separators=(",", ":"), ensure_ascii=False),))
    ids = [m.get("id") for m in new_members if isinstance(m, dict)] if isinstance(new_members, list) else []
    top = max([0, *(int(i[len("person"):]) for i in ids if isinstance(i, str) and i[len("person"):].isdigit())])
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'app_state'")
    if top and await cursor.fetchone() is not None:
        await conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value = CASE WHEN CAST(app_state.value AS INTEGER) < CAST(excluded.value AS INTEGER) "
            "THEN excluded.value ELSE app_state.value END", (LAST_MEMBER_KEY, str(top)))
