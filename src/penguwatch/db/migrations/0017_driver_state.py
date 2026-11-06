"""0017: each driver's app_state keys live under its own namespace, `driver.<name>.<key>`.

The SIMKL login and sync bookkeeping move from simkl_* keys to driver.simkl.* (the tokens included, so a stored
login keeps working without reconnecting). The activities snapshot a delta pull starts from is the driver's cursor.
The enrichment back-off is the sync engine's, not a driver's: sync.refresh_attempts.
"""

import aiosqlite

RENAMES = (
    ("simkl_client_id", "driver.simkl.client_id"),
    ("simkl_access_token", "driver.simkl.access_token"),
    ("simkl_refresh_token", "driver.simkl.refresh_token"),
    ("simkl_token_expires_at", "driver.simkl.token_expires_at"),
    ("simkl_token_renewed_at", "driver.simkl.token_renewed_at"),
    ("simkl_auth_state", "driver.simkl.auth_state"),
    ("simkl_auth_message", "driver.simkl.auth_message"),
    ("simkl_renewal_note", "driver.simkl.renewal_note"),
    ("simkl_scope", "driver.simkl.scope"),
    ("simkl_last_sync", "driver.simkl.last_sync"),
    ("simkl_last_error", "driver.simkl.last_error"),
    ("simkl_activities", "driver.simkl.cursor"),
    ("simkl_refresh_attempts", "sync.refresh_attempts"),
)


async def migrate(conn: aiosqlite.Connection) -> None:
    for old, new in RENAMES:
        # A key already under its new name (there is none before this migration) is kept; the old one goes.
        await conn.execute("UPDATE OR IGNORE app_state SET key = ? WHERE key = ?", (new, old))
        await conn.execute("DELETE FROM app_state WHERE key = ?", (old,))
