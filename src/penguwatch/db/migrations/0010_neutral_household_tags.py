"""0010: household tags are person1 / person2 / both (the names shown come from config.yaml). Any other stored value
(an empty one, or one from an older version) becomes person1, the default for a new title."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "UPDATE shows SET household_tag = 'person1' "
        "WHERE household_tag IS NULL OR household_tag NOT IN ('person1', 'person2', 'both')"
    )
