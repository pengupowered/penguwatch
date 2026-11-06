"""0006: service_price_history keeps every price a service has had; existing services get one row for today's price."""

from datetime import datetime, timezone

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS service_price_history ("
        "id INTEGER PRIMARY KEY, service_id TEXT NOT NULL, monthly_cost REAL NOT NULL, effective_from TEXT NOT NULL)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_service ON service_price_history(service_id, effective_from)"
    )
    cursor = await conn.execute("SELECT COUNT(*) FROM service_price_history")
    if (await cursor.fetchone())[0] == 0:
        await conn.execute(
            "INSERT INTO service_price_history (service_id, monthly_cost, effective_from) "
            "SELECT id, COALESCE(monthly_cost, 0.0), ? FROM services",
            (datetime.now(timezone.utc).isoformat(),),
        )
