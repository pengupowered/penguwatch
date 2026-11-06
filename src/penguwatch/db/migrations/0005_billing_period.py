"""0005: services.billing_period is 'monthly' or 'yearly' (monthly_cost stays the monthly equivalent)."""

import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(services)")
    existing = {row[1] for row in await cursor.fetchall()}
    if "billing_period" not in existing:
        await conn.execute("ALTER TABLE services ADD COLUMN billing_period TEXT DEFAULT 'monthly'")
