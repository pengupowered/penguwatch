"""Streaming-service queries for the repository."""

import json
from typing import Any, List, Optional, Sequence, Tuple

from penguwatch.models import ServiceConfig
from penguwatch.servicematch import match_service
from penguwatch.db.prices import record_price

# SQL predicate for queries aliasing `services` as `svc`: active, or covered by an active bundle.
EFFECTIVELY_ACTIVE_SQL = (
    "(svc.is_active = 1 OR EXISTS (SELECT 1 FROM services b, json_each(b.includes_json) j "
    "WHERE b.is_active = 1 AND j.value = svc.id))"
)


# The fields a release's catalogue may refresh on a service the user never edited (penguwatch.catalog): name, aliases,
# titles and bundle members. A change to one of them through the API marks the service "edited by you"
# (user_edited = 1), and nothing clears the marker.

def _refreshable(name: Any, aliases: Any, titles: Any, includes: Any) -> Tuple:
    return (name, list(aliases or []), list(titles or []), list(includes or []))


def _stored_refreshable(row) -> Tuple:
    def load(text):
        try:
            return json.loads(text) if text else []
        except ValueError:
            return []
    return _refreshable(row["name"], load(row["aliases_json"]), load(row["titles_json"]), load(row["includes_json"]))


def _normal(values: Sequence[str]) -> List[str]:
    return sorted({str(v).strip().casefold() for v in values or []})


def same_refreshable(a: ServiceConfig, b: ServiceConfig) -> bool:
    """Whether two services agree on the fields a catalogue refreshes, compared as a person would: the lists in any
    order, case and surrounding spaces ignored (the one-time import's rule for "edited by you")."""
    return (a.name.strip().casefold() == b.name.strip().casefold()
            and all(_normal(getattr(a, f)) == _normal(getattr(b, f)) for f in ("aliases", "titles", "includes")))


async def seed_services(db, services: Sequence[ServiceConfig], *, user_edited: bool) -> int:
    """Upserts `services` inside the caller's write transaction (the one-time settings import; it used to run at every
    start from repository.initialize): name, aliases, bundle members and titles are written; the price, active state,
    renewal and notes of an existing row are the user's and are kept; a new row starts its price history. A row is
    marked edited when `user_edited` (never unmarked). Returns the number of new rows."""
    added = 0
    for svc in services:
        before = await (await db.execute("SELECT monthly_cost FROM services WHERE id = ?", (svc.id,))).fetchone()
        await db.execute(
            """
            INSERT INTO services (id, name, monthly_cost, is_active, renewal_day, renewal_date, aliases_json, includes_json, notes, titles_json, user_edited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                aliases_json = excluded.aliases_json,
                includes_json = excluded.includes_json,
                titles_json = excluded.titles_json,
                user_edited = MAX(services.user_edited, excluded.user_edited)
            """,  # monthly_cost, is_active, renewal and notes are the user's: a price of 0 means "included", never "unset"
            (svc.id, svc.name, svc.monthly_cost, 1 if svc.is_active else 0, svc.renewal_day, svc.renewal_date,
             json.dumps(svc.aliases), json.dumps(svc.includes), svc.notes, json.dumps(svc.titles),
             1 if user_edited else 0),
        )
        if before is None:  # a brand-new service starts its price history; existing prices are the user's
            await record_price(db, svc.id, svc.monthly_cost, None)
            added += 1
    return added


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
        """Adds or replaces a service (POST /api/services, the Services tab's add and edit). A new service, or a change
        to its name, aliases, titles or bundle members, marks it "edited by you": a release's catalogue never
        refreshes it again. A price, renewal, note, billing or active change alone does not."""
        async with self.write() as db:
            row = await (await db.execute(
                "SELECT monthly_cost, name, aliases_json, titles_json, includes_json FROM services WHERE id = ?",
                (svc.id,))).fetchone()
            edited = row is None or _stored_refreshable(row) != _refreshable(svc.name, svc.aliases, svc.titles,
                                                                             svc.includes)
            await db.execute(
                """
                INSERT INTO services (id, name, monthly_cost, is_active, renewal_day, renewal_date, aliases_json, includes_json, notes, billing_period, titles_json, user_edited)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    titles_json = excluded.titles_json,
                    user_edited = MAX(services.user_edited, excluded.user_edited)
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
                    1 if edited else 0,
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
