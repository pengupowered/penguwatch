"""Streaming-service queries for the repository."""

import json
from typing import List, Optional

from penguwatch.models import ServiceConfig
from penguwatch.servicematch import match_service
from penguwatch.db.prices import record_price

# SQL predicate for queries aliasing `services` as `svc`: active, or covered by an active bundle.
EFFECTIVELY_ACTIVE_SQL = (
    "(svc.is_active = 1 OR EXISTS (SELECT 1 FROM services b, json_each(b.includes_json) j "
    "WHERE b.is_active = 1 AND j.value = svc.id))"
)


class ServicesMixin:
    # --- Services Operations ---

    def _row_to_service(self, r) -> ServiceConfig:
        aliases = json.loads(r["aliases_json"]) if r["aliases_json"] else []
        includes = []
        if "includes_json" in r.keys() and r["includes_json"]:
            includes = json.loads(r["includes_json"])
        titles = json.loads(r["titles_json"]) if "titles_json" in r.keys() and r["titles_json"] else []
        return ServiceConfig(
            id=r["id"],
            name=r["name"],
            monthly_cost=r["monthly_cost"] or 0.0,
            is_active=bool(r["is_active"]),
            renewal_day=r["renewal_day"],
            renewal_date=r["renewal_date"],
            aliases=aliases,
            includes=includes,
            titles=titles,
            notes=r["notes"],
            billing_period=(r["billing_period"] if "billing_period" in r.keys() else None) or "monthly",
        )

    async def get_services(self) -> List[ServiceConfig]:
        db = await self.read_connection()
        cursor = await db.execute("SELECT * FROM services ORDER BY name ASC")
        return [self._row_to_service(r) for r in await cursor.fetchall()]

    async def get_service(self, service_id: str) -> Optional[ServiceConfig]:
        db = await self.read_connection()
        cursor = await db.execute("SELECT * FROM services WHERE id = ?", (service_id,))
        r = await cursor.fetchone()
        return self._row_to_service(r) if r else None

    async def default_anime_service(self) -> Optional[str]:
        """'crunchyroll' if that service is configured, else None."""
        return "crunchyroll" if await self.get_service("crunchyroll") else None

    async def upsert_service(self, svc: ServiceConfig):
        async with self.write() as db:
            row = await (await db.execute("SELECT monthly_cost FROM services WHERE id = ?", (svc.id,))).fetchone()
            await db.execute(
                """
                INSERT INTO services (id, name, monthly_cost, is_active, renewal_day, renewal_date, aliases_json, includes_json, notes, billing_period, titles_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    monthly_cost = excluded.monthly_cost,
                    is_active = excluded.is_active,
                    renewal_day = excluded.renewal_day,
                    renewal_date = excluded.renewal_date,
                    aliases_json = excluded.aliases_json,
                    includes_json = excluded.includes_json,
                    notes = excluded.notes,
                    billing_period = excluded.billing_period,
                    titles_json = excluded.titles_json
                """,
                (
                    svc.id,
                    svc.name,
                    svc.monthly_cost,
                    1 if svc.is_active else 0,
                    svc.renewal_day,
                    svc.renewal_date,
                    json.dumps(svc.aliases),
                    json.dumps(svc.includes),
                    svc.notes,
                    svc.billing_period,
                    json.dumps(svc.titles),
                ),
            )
            await record_price(db, svc.id, svc.monthly_cost, None if row is None else (row["monthly_cost"] or 0.0))

    async def set_service_active(
        self,
        service_id: str,
        is_active: bool,
        renewal_day: Optional[int] = None,
        renewal_date: Optional[str] = None,
        clear_renewal_day: bool = False,
        clear_renewal_date: bool = False,
    ):
        """None keeps a stored renewal value; the clear_* flags set it to NULL."""
        async with self.write() as db:
            await db.execute(
                """
                UPDATE services
                SET is_active = ?,
                    renewal_day = CASE WHEN ? THEN NULL ELSE COALESCE(?, renewal_day) END,
                    renewal_date = CASE WHEN ? THEN NULL ELSE COALESCE(?, renewal_date) END
                WHERE id = ?
                """,
                (
                    1 if is_active else 0,
                    1 if clear_renewal_day else 0, renewal_day,
                    1 if clear_renewal_date else 0, renewal_date,
                    service_id,
                ),
            )

    async def match_service_by_network(
        self, network_name: Optional[str], title: Optional[str] = None,
        services: Optional[List[ServiceConfig]] = None,
    ) -> Optional[str]:
        """The configured service carrying `title` on `network_name` (see penguwatch.servicematch), or None.

        Pass `services` when matching many titles in a row, to read them once."""
        if services is None:
            services = await self.get_services()
        return match_service(services, network_name, title)
