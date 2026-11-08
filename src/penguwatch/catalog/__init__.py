"""The built-in streaming-services catalogue (services.yaml) and the refresh of the services nobody edited.

Contract 8.3. The first start seeds an empty table from the catalogue; every start refreshes the name, aliases, titles
and bundle members of catalogue services whose row is not marked edited (user_edited = 0). Price, renewal, notes,
billing period and active state belong to the user and are never touched; services are never added here.
"""

import json
from importlib import resources
from typing import List, Sequence

import aiosqlite
import yaml
from pydantic import ValidationError

from penguwatch.models import ServiceConfig

REFRESHED_FIELDS = ("name", "aliases", "titles", "includes")
CATALOG_FILE = "services.yaml"


class CatalogError(ValueError):
    """The packaged catalogue is broken (a build bug; tests pin it)."""


def load_catalog() -> List[ServiceConfig]:
    text = resources.files("penguwatch.catalog").joinpath(CATALOG_FILE).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise CatalogError(f"{CATALOG_FILE}: not valid YAML ({err.__class__.__name__})") from None
    rows = data.get("services") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise CatalogError(f"{CATALOG_FILE}: the top level needs a non-empty `services:` list")
    out: List[ServiceConfig] = []
    for n, row in enumerate(rows, 1):
        try:
            out.append(ServiceConfig(**row))
        except (ValidationError, TypeError) as err:
            raise CatalogError(f"{CATALOG_FILE}: service {n} is invalid ({err.__class__.__name__})") from None
    ids = [s.id for s in out]
    if len(set(ids)) != len(ids):
        raise CatalogError(f"{CATALOG_FILE}: duplicate service ids")
    return out


async def refresh_untouched(db: aiosqlite.Connection, catalog: Sequence[ServiceConfig]) -> List[str]:
    """Bring the refreshable fields of never-edited catalogue services up to the catalogue. Returns the ids changed,
    sorted. The caller holds the write lock and commits."""
    changed: List[str] = []
    for svc in catalog:
        row = await (await db.execute(
            "SELECT name, aliases_json, titles_json, includes_json FROM services WHERE id = ? AND user_edited = 0",
            (svc.id,))).fetchone()
        if row is None:
            continue
        new = (svc.name, json.dumps(svc.aliases), json.dumps(svc.titles), json.dumps(svc.includes))
        old = (row[0], row[1] or "[]", row[2] or "[]", row[3] or "[]")
        # compare decoded lists so formatting differences in stored JSON do not count as a change
        same = (old[0] == new[0] and all(json.loads(o) == json.loads(n) for o, n in zip(old[1:], new[1:])))
        if same:
            continue
        await db.execute(
            "UPDATE services SET name = ?, aliases_json = ?, titles_json = ?, includes_json = ? WHERE id = ?",
            (*new, svc.id))
        changed.append(svc.id)
    return sorted(changed)
