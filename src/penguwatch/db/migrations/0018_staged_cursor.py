"""0018: changes staged for review keep the pull position they wait on under "cursor" (the driver's own cursor),
not "activities" (what SIMKL calls it). The entries themselves (the library's records) are unchanged."""

import json

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("SELECT value FROM app_state WHERE key = 'incoming_staged'")
    row = await cursor.fetchone()
    if row is None:
        return
    try:
        staged = json.loads(row[0])
    except ValueError:
        return
    if isinstance(staged, dict) and "activities" in staged and "cursor" not in staged:
        staged = {("cursor" if k == "activities" else k): v for k, v in staged.items()}
        await conn.execute("UPDATE app_state SET value = ? WHERE key = 'incoming_staged'", (json.dumps(staged),))
