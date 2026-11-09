"""Reading settings out of a config.yaml mapping: the one-time import at the first start of the settings store
(`run_import`), and Settings > Import (an export, or a whole config.yaml, read into a draft: `read_settings`).

The file is read leniently (settings/lenient.py): every value is validated with the other values of its section, and
one the models refuse is left out and reported by its key with the reason, never with the value. A value the file
sets is stored even when it equals the default: the owner wrote it. A key the file does not set is stored only at the
one-time import, and only when what the old build ran with differs from the new default (import rule 2,
penguwatch.settings.legacy), so behaviour does not change at the upgrade. The environment is never copied in.

Left alone: the bootstrap keys (server.host, server.port, database.db_path; they stay in config.yaml).

Webhooks: each gets an id from its name (or its URL's host, as the old build named it), unique. At the one-time
import its `url` moves to secrets.json (slot `webhook.<id>.url`); `secret_env` / `secret_file` are kept as written
when they pass penguwatch.webhooks.check_secret_env / check_secret_file (a PENGUWATCH_WEBHOOK_* variable, a file under
/run/secrets/); a hook with another one is left out and reported. Settings > Import never brings a secret in: those
URLs are left out with a note.

Not settings, handed over once by the one-time import, in its transaction: the streaming services (`services:`, or
the packaged catalogue for an empty table, with the "edited by you" marker), the custom rating sources
(`ratings.sources`, then editable on the Ratings tab) and a driver login written in the file (into the stored login,
when that is empty).
"""

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from pydantic import ValidationError

from penguwatch.catalog import CatalogError, load_catalog, refresh_untouched
from penguwatch.config import BOOTSTRAP_KEYS, AppConfig, WebhookConfig, WebhookItem, webhook_id
from penguwatch.drivers import DRIVERS
from penguwatch.drivers.base import state_key
from penguwatch.models import ServiceConfig
from penguwatch.ratings import RatingSourceSpec
from penguwatch.settings import legacy, lenient, registry
from penguwatch.settings.document import SettingsDocument
from penguwatch.webhooks import check_secret_env, check_secret_file

_log = logging.getLogger("penguwatch.settings")

MAX_UNKNOWN_NOTES = 8
SECRETISH = re.compile(r"url|token|password|secret", re.I)
NOT_SETTINGS = ("services", "ratings")  # their own tables: handed over once, then edited on their tabs


@dataclass
class ReadResult:
    document: SettingsDocument
    report: List[Dict[str, str]] = field(default_factory=list)  # [{key, reason}]: left out; never a value
    notes: List[str] = field(default_factory=list)  # what was dropped or left in config.yaml on purpose
    secret_updates: Dict[str, Any] = field(default_factory=dict)  # merged into secrets.json (one-time import only)
    slots: List[str] = field(default_factory=list)  # the slots secret_updates writes (names)
    services: Optional[List[ServiceConfig]] = None  # the file's `services:` (None: the file has none)
    rating_sources: Optional[List[RatingSourceSpec]] = None  # the file's `ratings.sources` (None: none)
    credentials: Dict[Tuple[str, str], str] = field(default_factory=dict, repr=False)  # (driver, option) -> value

    def summary(self, services: Optional[int] = None) -> str:
        """The version line: "Imported from config.yaml: 11 values; webhooks 1; services 14"."""
        doc = self.document
        parts = []
        hooks = doc.lists.get("webhooks")
        if hooks is not None:
            parts.append(f"webhooks {len(hooks)}")
        if services:
            parts.append(f"services {services}")
        if not doc.values and not parts:
            return "Started with the defaults"
        values = f"{len(doc.values)} value{'s' if len(doc.values) != 1 else ''}"
        return "Imported from config.yaml: " + "; ".join([values, *parts])


# --- reading paths -------------------------------------------------------------------------------------------------

def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def get_path(config: AppConfig, key: str) -> Any:
    """The value of registry key (or config path) `key` in `config` (a driver's options under the driver's name)."""
    first, *rest = key.split(".")
    obj = config.driver_options(first) if first in registry.driver_option_models() else getattr(config, first)
    for part in rest:
        obj = getattr(obj, part)
    return obj


def present(raw: Any, path: str) -> bool:
    """Whether the mapping sets the dotted path (every parent a mapping, the leaf present and not null)."""
    node = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return node is not None


def _pop(raw: dict, path: str) -> Any:
    parts = path.split(".")
    node = raw
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    return node.pop(parts[-1], None) if isinstance(node, dict) else None


def _as_stored(setting: "registry.Setting", value: Any) -> Any:
    """An old build's value in the shape this build stores (the frozen legacy tables keep the old shapes: the
    household's members were a list of names before 0.17): read by the setting's own section model; as it is when
    that model refuses it."""
    model, name, _ = setting.resolve()
    try:
        return jsonable(getattr(model.model_validate({name: copy.deepcopy(value)}), name))
    except (ValidationError, ValueError, TypeError):
        return copy.deepcopy(value)


def _dropped(paths: Iterable[str], key: str) -> bool:
    return any(p == key or key.startswith(p + ".") for p in paths)


def credential_options() -> List[Tuple[str, str]]:
    """(driver, option) of the driver options that are a stored login: on no page, kept in app_state
    (state_key(driver, option)) by the driver's own "Save credentials"."""
    return [(name, option) for name, driver in sorted(DRIVERS.items())
            for option in getattr(driver, "not_on_page", {}) if option in getattr(driver, "secret_state_keys", ())]


# --- the parts that are not settings ------------------------------------------------------------------------------

def _webhooks(raw_hooks: Any, move_secrets: bool, result: ReadResult) -> Optional[List[dict]]:
    """The webhook items (never a URL); the URLs go to secret_updates (one-time import) or are left out with a note
    (Settings > Import)."""
    if raw_hooks is None:
        return None
    if not isinstance(raw_hooks, list):
        result.report.append({"key": "webhooks", "reason": "must be a list"})
        return None
    parsed: List[Tuple[str, Any]] = []
    urls_left_out = 0
    for i, row in enumerate(raw_hooks):
        where = f"webhooks[{i}]"
        if not isinstance(row, dict):
            result.report.append({"key": where, "reason": "must be a mapping"})
            continue
        row = dict(row)
        if not move_secrets and row.pop("url", None):
            urls_left_out += 1
        try:
            parsed.append((where, (WebhookConfig if move_secrets else WebhookItem).model_validate(row)))
        except ValidationError as e:
            result.report.append({"key": where, "reason": lenient.reason(e.errors()[0])})
    given_ids = {hook.id for _, hook in parsed if hook.id}
    names: Set[str] = set()
    taken: Set[str] = set()
    items: List[dict] = []
    for number, (where, hook) in enumerate(parsed, start=1):
        url = getattr(hook, "url", None)
        hook.name = hook.name or ((urlsplit(url).hostname or "")[:40] if url else "") or f"webhook-{number}"
        if hook.name in names:
            result.report.append({"key": where, "reason": "the name is used twice"})
            continue
        if hook.id and hook.id in taken:
            result.report.append({"key": where, "reason": "the id is used twice"})
            continue
        if len(items) >= registry.MAX_WEBHOOKS:
            result.report.append({"key": where, "reason": f"at most {registry.MAX_WEBHOOKS} webhooks"})
            continue
        # an imported secret_env / secret_file is kept as written only when it is a webhook variable or a container
        # secret (penguwatch.webhooks); otherwise the hook is left out: without its secret it would send unsigned
        problem = next(((f, m) for f, m in (("secret_env", hook.secret_env and check_secret_env(hook.secret_env)),
                                            ("secret_file", hook.secret_file and check_secret_file(hook.secret_file)))
                        if m), None)
        if problem:
            result.report.append({"key": f"{where}.{problem[0]}", "reason": problem[1]})
            continue
        names.add(hook.name)
        hook.id = hook.id or webhook_id(hook.name, taken | given_ids)
        taken.add(hook.id)
        if move_secrets:
            result.secret_updates.setdefault("webhooks", {})[hook.id] = {"url": url}
            result.slots.append(f"webhook.{hook.id}.url")
        item = WebhookItem.model_validate(hook.model_dump(include=set(WebhookItem.model_fields)))
        items.append(item.model_dump(mode="json"))
    if urls_left_out:
        result.notes.append(f"{urls_left_out} webhook address{'es' if urls_left_out != 1 else ''} in the file "
                            f"{'were' if urls_left_out != 1 else 'was'} not imported: addresses and secrets are "
                            "entered on the page")
    return items


def _services(raw: Any, result: ReadResult) -> Optional[List[ServiceConfig]]:
    if raw is None:
        return None
    if not isinstance(raw, list):
        result.report.append({"key": "services", "reason": "must be a list"})
        return None
    out: List[ServiceConfig] = []
    seen: Set[str] = set()
    for i, row in enumerate(raw):
        where = f"services[{i}]"
        try:
            service = ServiceConfig.model_validate(row)
        except ValidationError as e:
            result.report.append({"key": where, "reason": lenient.reason(e.errors()[0])})
            continue
        if service.id in seen:
            result.report.append({"key": where, "reason": "the id is used twice"})
            continue
        seen.add(service.id)
        out.append(service)
    return out


def _rating_sources(raw: Any, result: ReadResult) -> Optional[List[RatingSourceSpec]]:
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if sources is None:
        return None
    if not isinstance(sources, list):
        result.report.append({"key": "ratings.sources", "reason": "must be a list"})
        return None
    out: List[RatingSourceSpec] = []
    seen: Set[str] = set()
    for i, row in enumerate(sources):
        where = f"ratings.sources[{i}]"
        try:
            spec = RatingSourceSpec.model_validate(row)
        except ValidationError as e:
            result.report.append({"key": where, "reason": lenient.reason(e.errors()[0])})
            continue
        if spec.id in seen:
            result.report.append({"key": where, "reason": "the id is used twice"})
            continue
        seen.add(spec.id)
        out.append(spec)
    return out


def _unknown_notes(data: dict) -> List[str]:
    """A note for every key of the file that is no setting of this build (it is left out)."""
    known = set(registry.stored_leaf_paths()) | {"ratings"}
    unknown: List[str] = []

    def walk(node: Any, path: str) -> None:
        if path in known:
            return
        if isinstance(node, dict) and node:
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif path:
            unknown.append(path)

    walk(data, "")
    notes = [f"{p}: secrets are entered on the page, not read from a file" if SECRETISH.search(p)
             else f"{p}: not a setting of this version" for p in unknown[:MAX_UNKNOWN_NOTES]]
    if len(unknown) > MAX_UNKNOWN_NOTES:
        notes.append(f"and {len(unknown) - MAX_UNKNOWN_NOTES} more keys that are not settings of this version")
    return notes


# --- reading -------------------------------------------------------------------------------------------------------

def read_settings(raw: Any, *, move_secrets: bool = False, old: Optional[Dict[str, Any]] = None) -> ReadResult:
    """The settings a config.yaml mapping (or an export) sets. `move_secrets`: the one-time import (webhook URLs go
    to `secret_updates`, services, rating sources and logins are read). `old`: what the old build ran with for a key
    the file leaves out (legacy.old_values; the one-time import only): stored when it differs from the default."""
    result = ReadResult(SettingsDocument())
    if not isinstance(raw, dict):
        result.report.append({"key": "(file)", "reason": "must be a YAML mapping"})
        return result
    data = copy.deepcopy(raw)
    raw_hooks = data.pop("webhooks", None)
    raw_services = data.pop("services", None)
    raw_ratings = data.pop("ratings", None)
    for driver, option in credential_options():
        value = _pop(data, f"{driver}.{option}")
        if move_secrets and isinstance(value, str) and value.strip():
            result.credentials[(driver, option)] = value.strip()
    left = [key for key in BOOTSTRAP_KEYS if present(raw, key)]
    if left:
        result.notes.append(f"left in config.yaml: {', '.join(left)}")
    result.notes += _unknown_notes(data)

    config, dropped = lenient.validate_leniently(AppConfig, data)
    gone = lenient.removed_paths(dropped)
    result.report += [{"key": path, "reason": why} for path, why in dropped
                      if not any(path == k or k.startswith(path + ".") for k in BOOTSTRAP_KEYS)]

    values: Dict[str, Any] = {}
    for setting in registry.SETTINGS:
        if present(data, setting.key):
            if not _dropped(gone, setting.key):
                values[setting.key] = jsonable(get_path(config, setting.key))
        elif old is not None and setting.key in old:
            was = _as_stored(setting, old[setting.key])
            if was != jsonable(setting.default()):
                values[setting.key] = was  # import rule 2: keep what the old build ran with

    lists: Dict[str, Optional[List[dict]]] = {}
    items = _webhooks(raw_hooks, move_secrets, result)
    if items is not None:
        lists["webhooks"] = items
    if move_secrets:
        result.services = _services(raw_services, result)
        result.rating_sources = _rating_sources(raw_ratings, result)
    else:
        for name in NOT_SETTINGS:
            if raw.get(name) is not None:
                where = "the Services & Sync tab" if name == "services" else "the Ratings tab"
                result.notes.append(f"{name}: not imported; edited on {where}")
    result.document = SettingsDocument(values=values, lists=lists)
    return result


# --- the catalogue (penguwatch.catalog) -----------------------------------------------------------------------------

async def refresh_catalogue(repository) -> list:
    """The release's catalogue values for the services the user never edited (name, aliases, titles, bundle members;
    never a price or the active state). A broken catalogue is logged and the start goes on."""
    try:
        catalog = load_catalog()
    except CatalogError as e:
        _log.error(f"The packaged catalogue cannot be read ({e}); no service was refreshed")
        return []
    async with repository.write() as db:
        changed = await refresh_untouched(db, catalog)
    if changed:
        _log.info(f"Catalogue refreshed {len(changed)} services: {', '.join(changed)}")
    return changed


# --- the one-time import -------------------------------------------------------------------------------------------

@dataclass
class ImportOutcome:
    imported: bool  # False: the store was initialized before (nothing written)
    summary: str = ""
    report: List[Dict[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


async def _hand_over(db, result: ReadResult, catalog: Optional[List[ServiceConfig]], report: List[Dict[str, str]],
                     notes: List[str]) -> None:
    """The parts of the file that are not settings, inside the import's transaction."""
    from penguwatch.db.ratings import import_rating_sources
    from penguwatch.db.services import same_refreshable, seed_services

    by_id = {s.id: s for s in catalog} if catalog is not None else None
    if result.services is not None:
        # a file service is the user's own unless it equals the catalogue's entry
        untouched = [s for s in result.services if by_id is not None and s.id in by_id
                     and same_refreshable(s, by_id[s.id])]
        edited = [s for s in result.services if s not in untouched]
        await seed_services(db, untouched, user_edited=False)
        await seed_services(db, edited, user_edited=True)
    elif catalog:
        empty = (await (await db.execute("SELECT COUNT(*) FROM services")).fetchone())[0] == 0
        if empty:
            await seed_services(db, catalog, user_edited=False)
    if by_id is None:  # no catalogue to compare with: nothing may be refreshed, so every service is the user's
        await db.execute("UPDATE services SET user_edited = 1")
    else:  # a service the catalogue does not have was added by the user (the tab or the API)
        marks = ", ".join("?" for _ in by_id)
        await db.execute(f"UPDATE services SET user_edited = 1 WHERE id NOT IN ({marks})" if by_id
                         else "UPDATE services SET user_edited = 1", tuple(by_id))
    report += await import_rating_sources(db, result.rating_sources or [])
    for (driver, option), value in result.credentials.items():
        key = state_key(driver, option)
        row = await (await db.execute("SELECT value FROM app_state WHERE key = ?", (key,))).fetchone()
        label = f"{driver}.{option}"
        if row is not None and row[0]:
            notes.append(f"{label}: the stored login is kept; the file's value was not used")
            continue
        await db.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        notes.append(f"{label}: moved to the stored login")


async def run_import(repository, secrets, raw: Any, config_path: Optional[str]) -> ImportOutcome:
    """The one-time import of config.yaml (`raw`, read from `config_path`; None: the packaged example) into the
    settings store, when it was never initialized. Webhook addresses go to secrets.json first (a crash before the
    commit re-runs the import, which writes the same entries: the ids are deterministic); then one transaction holds
    the values, the webhooks, the services, the rating sources, a file-held login, revision 1 and its version. A
    failure rolls the transaction back and propagates (the start stops; the next start tries again)."""
    if await repository.settings_initialized():
        return ImportOutcome(imported=False)
    result = read_settings(raw, move_secrets=True, old=legacy.old_values(legacy.ran_on_example(config_path)))
    try:
        catalog: Optional[List[ServiceConfig]] = load_catalog()
    except CatalogError as e:
        _log.error(f"The packaged catalogue cannot be read ({e}); every imported service counts as edited by you")
        catalog = None
    if result.secret_updates:
        secrets.change(result.secret_updates)
        _log.info(f"Moved {len(result.slots)} webhook address(es) from config.yaml to {secrets.path}")
    services = len(result.services) if result.services is not None else None
    if services is None and catalog and not await repository.get_services():
        services = len(catalog)
    summary = result.summary(services)
    report, notes = list(result.report), list(result.notes)

    async def also(db) -> None:
        await _hand_over(db, result, catalog, report, notes)

    if not await repository.initialize_settings(result.document, summary, report, also=also, slots=result.slots):
        return ImportOutcome(imported=False)
    line = summary.replace("Imported from config.yaml", "Imported settings from config.yaml")
    if report:
        line += f"; {len(report)} left out (defaults kept): " + ", ".join(r["key"] for r in report)
    _log.info(line)
    for note in notes:
        _log.info(f"Settings import: {note}")
    return ImportOutcome(imported=True, summary=summary, report=report, notes=notes)
