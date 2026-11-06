"""Price history of the streaming services (every price a service has had, newest first)."""

from typing import Any, Dict, List

import aiosqlite

from penguwatch.db._common import _utcnow


async def record_price(db: aiosqlite.Connection, service_id: str, monthly_cost: float, previous: Any) -> None:
    """Adds a history row when the price differs from `previous` (None for a brand-new service).

    Runs inside the caller's write transaction.
    """
    if previous is None or abs(float(previous) - float(monthly_cost)) > 1e-9:
        await db.execute(
            "INSERT INTO service_price_history (service_id, monthly_cost, effective_from) VALUES (?, ?, ?)",
            (service_id, monthly_cost, _utcnow()),
        )


class PricesMixin:
    async def get_price_history(self, service_id: str) -> List[Dict[str, Any]]:
        """Every price of one service, newest first."""
        db = await self.read_connection()
        cursor = await db.execute(
            "SELECT monthly_cost, effective_from FROM service_price_history WHERE service_id = ? "
            "ORDER BY effective_from DESC, id DESC",
            (service_id,),
        )
        return [{"monthly_cost": r["monthly_cost"], "effective_from": r["effective_from"]} for r in await cursor.fetchall()]
