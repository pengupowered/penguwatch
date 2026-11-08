"""The settings manager: the settings store at run time (ctx.settings_manager, container.settings).

At start (`load`): the one-time import of config.yaml when the store was never initialized (settings/importer.py),
then the stored document over the model defaults, the environment on top, copied into the live config (`ctx.config`)
in place. After that config.yaml is only read for the bootstrap keys (config.BOOTSTRAP_KEYS); one log line says how
many of its settings keys differ from the store (names only).

A save (`apply`): validate the change as a whole candidate config (settings/validate.py); under one lock: the conflict
check, the secret slots to secrets.json (one atomic write), the document as the next revision in one database
transaction (db/settings.py, with its version; when that fails, secrets.json is put back as it was), then go live:
copy the new effective values of the live settings into the live objects in one synchronous block and call the hub's
appliers for what running parts keep a copy of. Settings with `live: False` (the drivers, TVmaze's client limits, the
app name) keep their running values until the next start (`pending_restart`).

The document, its versions, the export and every API answer hold no secret: the slots live in secrets.json and only
their state (set or not, where from, a URL's host) goes out.
"""

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

import yaml

from penguwatch import telemetry
from penguwatch.config import BOOTSTRAP_KEYS, AppConfig, EnvOverrides, apply_env, env_pins
from penguwatch.db.settings import SettingsConflict
from penguwatch.secrets import SECRETS_FILE_DAMAGED, SecretsFileUnusable, SecretStore, read_secret_file, slot_path
from penguwatch.settings import lenient, registry
from penguwatch.settings import yamlsafe
from penguwatch.settings.document import SettingsDocument
from penguwatch.settings.env import SECRET_ENV, env_value
from penguwatch.settings.hub import Change, SettingsHub
from penguwatch.settings.importer import get_path, jsonable, read_settings, run_import
from penguwatch.settings.plain import plain
from penguwatch.settings.validate import Validation, list_item_id
from penguwatch.settings.validate import validate as validate_change
from penguwatch.webhooks import MIN_SECRET_LENGTH

_log = logging.getLogger("penguwatch.settings")

EXPORT_HEADER = ("# PenguWatch settings (Settings > Export)\n"
                 "# Import them on the Settings tab of any install. No secret is in this file: the digest address and\n"
                 "# the webhooks' addresses and signing secrets are entered on the page.\n")
SOURCES = ("save", "restore", "yaml_import")


class SettingsInvalid(Exception):
    """A change the validation refused. `errors`: [{"where", "message"}]; `status` 422, or 400 for a write to a part an
    environment variable pins."""

    def __init__(self, errors: List[Dict[str, str]], status: int = 422, detail: str = "Some values were refused"):
        super().__init__(detail)
        self.errors, self.status, self.detail = errors, status, detail


class SettingsWriteFailed(Exception):
    """secrets.json or the database could not be written; nothing was saved. `detail` says why, for the page."""

    def __init__(self, detail: str = "The secrets file could not be written; nothing was saved"):
        super().__init__(detail)
        self.detail = detail


@dataclass
class Applied:
    revision: int
    summary: str
    applied: List[str]
    restart_needed: List[str] = field(default_factory=list)
    live_failed: List[str] = field(default_factory=list)
    reload: bool = False


# --- dotted paths into plain mappings ----------------------------------------------------------------------------

def set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def host_of(url: Optional[str]) -> Optional[str]:
    try:
        return urlsplit(url).hostname if url else None
    except ValueError:
        return None


def _set_key(config: AppConfig, key: str, value: Any) -> None:
    first, *middle, last = key.split(".")
    obj = config.driver_options(first) if first in registry.driver_option_models() else getattr(config, first)
    for part in middle:
        obj = getattr(obj, part)
    setattr(obj, last, value)


def copy_into(live: AppConfig, new: AppConfig, *, restart_keys: bool = False) -> Set[str]:
    """Copies `new`'s settings into the live config objects in place (every holder of a section sees the change):
    each registry key whose value changed (`live: False` keys only with `restart_keys`, which the start does: the
    drivers are not built yet) and the webhooks by slice. One synchronous block. Returns the keys changed."""
    changed: Set[str] = set()
    for setting in registry.SETTINGS:
        if not setting.live and not restart_keys:
            continue
        value = get_path(new, setting.key)
        if jsonable(get_path(live, setting.key)) != jsonable(value):
            _set_key(live, setting.key, copy.deepcopy(value))
            changed.add(setting.key)
    live.webhooks[:] = new.webhooks
    return changed


# --- the manager ---------------------------------------------------------------------------------------------------

class SettingsManager:
    def __init__(self, config: AppConfig, repository, secrets: SecretStore, hub: Optional[SettingsHub] = None,
                 env: Optional[EnvOverrides] = None, environ: Optional[Mapping[str, str]] = None, *,
                 config_path: Optional[str] = None, file_raw: Optional[dict] = None,
                 token_set: Callable[[], bool] = lambda: False):
        """`config` is the live config (ctx.config): the start and every save copy into it. `env`: the variables read
        at this start (pins are checked by their fields; a refused one is ignored). `environ`: where a webhook's
        `secret_env` is looked up (default os.environ). `config_path` / `file_raw`: the config.yaml read for the
        one-time import and the start-up comparison (None path: the packaged example)."""
        self.config = config
        self.repository = repository
        self.secrets = secrets
        self.hub = hub or SettingsHub()
        self.env = env if env is not None else EnvOverrides()
        self._environ = environ
        self.config_path = config_path
        self._raw = file_raw if file_raw is not None else {}
        self.token_set = token_set
        self.revision = 0
        self.document = SettingsDocument()
        self._effective: AppConfig = config
        self._start_values: Dict[str, Any] = {}
        self._live_failed: Set[str] = set()
        self._import_info: Dict[str, Any] = {"report": [], "imported_at": None}
        self._differing: List[str] = []
        self._differing_services = 0
        self.refused_keys: Set[str] = set()  # stored values this build's ranges refuse (their defaults run)
        self._lock = asyncio.Lock()

    # --- the environment ---------------------------------------------------------------------------------------------

    def pinned(self) -> Dict[str, Any]:
        """{registry key: value} of the settings a valid variable pins right now."""
        return env_pins(self.env)[0]

    def env_ignored(self) -> Dict[str, str]:
        """{registry key: why its variable is ignored} (set but refused by the field; never the value)."""
        return env_pins(self.env)[1]

    def secret_env(self, slot: str) -> Optional[str]:
        """The variable that pins a slot while it is set (a write to the slot is refused), or None."""
        variable = SECRET_ENV.get(slot)
        if variable and (self.env.notify_url or "").strip():
            return variable
        return None

    # --- secrets -------------------------------------------------------------------------------------------------------

    def stored_secret(self, slot: str) -> Optional[str]:
        """The slot's value in secrets.json, or None. Never log it."""
        path = slot_path(slot)
        return self.secrets.get(path) if path is not None else None

    def secret_value(self, slot: str) -> Optional[str]:
        """The value a running part sends with: notify.url from PENGUWATCH_NOTIFY_URL, else secrets.json; a webhook's
        URL and stored signing secret from secrets.json (its secret_env / secret_file are resolved by the webhooks
        component, as before). Never log or return it from the API."""
        if slot == "notify.url" and (self.env.notify_url or "").strip():
            return self.env.notify_url.strip()
        return self.stored_secret(slot)

    def _hook_item(self, hook: str) -> Optional[dict]:
        return next((i for i in self.document.lists.get("webhooks") or [] if list_item_id(i) == hook), None)

    def secret_state(self, slot: str) -> Dict[str, Any]:
        """{"set", "source", "host"} of a slot: never its value."""
        url = slot.endswith(".url")
        if slot.startswith("webhook.") and slot.endswith(".secret"):
            item = self._hook_item(slot[len("webhook."):-len(".secret")]) or {}
            if item.get("secret_env"):
                return {"set": env_value(item["secret_env"], self._environ) is not None, "source": "env", "host": None}
            if item.get("secret_file"):
                return {"set": read_secret_file(Path(item["secret_file"])) is not None, "source": "file",
                        "host": None}
        if self.secret_env(slot):
            value = self.secret_value(slot)
            return {"set": True, "source": "env", "host": host_of(value) if url else None}
        value = self.stored_secret(slot)
        if value is None:
            return {"set": False, "source": None, "host": None}
        return {"set": True, "source": "stored", "host": host_of(value) if url else None}

    def slots(self) -> List[str]:
        out = ["notify.url"]
        for item in self.document.lists.get("webhooks") or []:
            out += [f"webhook.{list_item_id(item)}.url", f"webhook.{list_item_id(item)}.secret"]
        return out

    def secret_states(self) -> Dict[str, Dict[str, Any]]:
        return {slot: self.secret_state(slot) for slot in self.slots()}

    # --- reading -------------------------------------------------------------------------------------------------------

    def effective_value(self, key: str) -> Any:
        return jsonable(get_path(self._effective, key))

    def values(self) -> Dict[str, Any]:
        """{registry key: effective value} (env > stored > default)."""
        return {s.key: self.effective_value(s.key) for s in registry.SETTINGS}

    def value_states(self) -> Dict[str, Dict[str, Any]]:
        pinned, ignored = env_pins(self.env)
        out = {}
        for setting in registry.SETTINGS:
            key = setting.key
            value = self.effective_value(key)
            source = "env" if key in pinned else "stored" if key in self.document.values else "default"
            running = jsonable(get_path(self.config, key))
            out[key] = {"value": value, "stored": copy.deepcopy(self.document.values.get(key)), "source": source,
                        "pending_restart": not setting.live and value != running,
                        "live_failed": key in self._live_failed, "env_ignored": ignored.get(key)}
        return out

    def list_items(self) -> List[dict]:
        return copy.deepcopy(self.document.lists.get("webhooks") or [])

    def _hook_state(self, item: dict, running: Mapping[str, Any]) -> Dict[str, Any]:
        hook = list_item_id(item)
        if self.stored_secret(f"webhook.{hook}.url") is None:
            return {"state": "address_needed", "reason": None}
        if item.get("secret_env"):
            value = env_value(item["secret_env"], self._environ)
            if value is None:
                return {"state": "disabled", "reason": f"environment variable {item['secret_env']} is not set"}
            if len(value) < MIN_SECRET_LENGTH:
                return {"state": "disabled", "reason": f"the secret in environment variable {item['secret_env']} is "
                                                       f"shorter than {MIN_SECRET_LENGTH} characters"}
        elif item.get("secret_file"):
            value = read_secret_file(Path(item["secret_file"]))
            if value is None:
                return {"state": "disabled", "reason": f"secret file {item['secret_file']} cannot be read"}
            if len(value.strip()) < MIN_SECRET_LENGTH:
                return {"state": "disabled", "reason": f"the secret in secret file {item['secret_file']} is shorter "
                                                       f"than {MIN_SECRET_LENGTH} characters"}
        reason = getattr(running.get(item.get("name")), "disabled_reason", None)
        return {"state": "disabled", "reason": reason} if reason else {"state": "ok", "reason": None}

    def list_states(self, running_hooks=()) -> Dict[str, Dict[str, Any]]:
        running = {getattr(h, "name", None): h for h in running_hooks}
        items = self.list_items()
        return {"webhooks": {"set": self.document.lists.get("webhooks") is not None, "items": items,
                             "states": {list_item_id(i): self._hook_state(i, running) for i in items}}}

    def import_report(self) -> List[Dict[str, str]]:
        return [r for r in self._import_info.get("report") or []
                if isinstance(r.get("key"), str) and isinstance(r.get("reason"), str)]

    def config_yaml_state(self) -> Dict[str, Any]:
        return {"ignored_since": self._import_info.get("imported_at"), "differing_keys": len(self._differing),
                "differing_services": self._differing_services}

    @property
    def differing_keys(self) -> List[str]:
        return list(self._differing)

    async def titles_for_others(self) -> int:
        """Titles tagged for the second person or both (the household warning)."""
        db = await self.repository.read_connection()
        cursor = await db.execute("SELECT COUNT(*) FROM shows WHERE household_tag IN ('person2', 'both')")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # --- the effective config ------------------------------------------------------------------------------------------

    def candidate(self, document: SettingsDocument, secret: Callable[[str], Optional[str]],
                  problems: Optional[List[Dict[str, str]]] = None) -> dict:
        """The config of `document` as a mapping: the bootstrap keys of the live config, the document's values and
        the webhooks (each with its URL from its slot; one without is left out)."""
        data: dict = {}
        for key in BOOTSTRAP_KEYS:
            set_path(data, key, jsonable(get_path(self.config, key)))
        for key, value in document.values.items():
            if key in registry.BY_KEY:
                set_path(data, key, copy.deepcopy(value))
        items = document.lists.get("webhooks")
        if items is not None:
            hooks = []
            for item in items:
                url = secret(f"webhook.{list_item_id(item)}.url")
                if url is None:
                    if problems is not None:
                        problems.append({"where": f"secrets.webhook.{list_item_id(item)}.url",
                                         "message": "address needed"})
                    continue
                hooks.append({**copy.deepcopy(item), "url": url})
            data["webhooks"] = hooks
        return data

    def effective(self, document: Optional[SettingsDocument] = None) -> AppConfig:
        """The effective config of `document` (default: the stored one): bootstrap keys from the running config, the
        document over the model defaults (lenient: a stored value this build refuses costs that value only), the
        environment on top (a pinning variable its field refuses is ignored), each webhook with its URL slot."""
        return self.build(document)[0]

    def build(self, document: Optional[SettingsDocument] = None) -> Tuple[AppConfig, List[Tuple[str, str]]]:
        """(the effective config of `document`, [(path, reason)] left out of it)."""
        document = document if document is not None else self.document
        data = self.candidate(document, self.stored_secret)
        config, dropped = lenient.validate_leniently(AppConfig, data)
        if document is self.document:
            self.refused_keys = {k for k in document.values
                                 if any(p == k or k.startswith(p + ".") for p in lenient.removed_paths(dropped))}
        config.services = self.config.services  # not settings: their own table (never read from here)
        config.ratings = self.config.ratings
        return apply_env(config, self.env), dropped

    # --- start ---------------------------------------------------------------------------------------------------------

    async def load(self) -> None:
        """The one-time import (first start of the store), then the stored document into the live config (every
        key: the drivers are not built yet)."""
        if not await self.repository.settings_initialized():
            try:
                outcome = await run_import(self.repository, self.secrets, self._raw, self.config_path)
            except Exception:
                telemetry.SETTINGS_IMPORTS.add(1, {"kind": "config_yaml", "outcome": "failed"})
                raise
            if outcome.imported:
                telemetry.SETTINGS_IMPORTS.add(1, {"kind": "config_yaml", "outcome": "imported"})
        revision, stored = await self.repository.load_settings()
        unknown = sorted([k for k in stored.values if k not in registry.BY_KEY]
                         + [n for n in stored.lists if n not in registry.LISTS])
        if unknown:
            _log.info(f"Stored settings this build does not know are kept and ignored: {', '.join(unknown)}")
        self.revision = revision
        self.document = self._known(stored)
        config, dropped = self.build()
        if dropped:
            _log.warning(f"Stored settings refused by this build (their defaults are used): "
                         f"{', '.join(lenient.removed_paths(dropped))}")
        copy_into(self.config, config, restart_keys=True)
        self._effective = config
        self._start_values = self.values()
        self._import_info = await self.repository.settings_import_info()
        await self._compare_file()

    @staticmethod
    def _known(stored: SettingsDocument) -> SettingsDocument:
        return SettingsDocument(values={k: v for k, v in stored.values.items() if k in registry.BY_KEY},
                                lists={n: v for n, v in stored.lists.items() if n in registry.LISTS})

    async def _compare_file(self) -> None:
        """One line when config.yaml's settings keys differ from the store: key names and counts only, never values."""
        raw = self._raw if isinstance(self._raw, dict) else {}
        file = read_settings(raw, move_secrets=True)
        differing = []
        for key, value in file.document.values.items():
            stored = self.document.values.get(key, jsonable(registry.get(key).default()))
            if value != stored:
                differing.append(key)
        items = file.document.lists.get("webhooks")
        if items is not None:
            urls = {hook: entry.get("url") for hook, entry in (file.secret_updates.get("webhooks") or {}).items()}
            if items != (self.document.lists.get("webhooks") or []) or any(
                    self.stored_secret(f"webhook.{hook}.url") != url for hook, url in urls.items()):
                differing.append("webhooks")
        self._differing = sorted(differing)
        self._differing_services = 0
        if file.services is not None:
            table = {s.id: s for s in await self.repository.get_services()}
            self._differing_services = sum(
                1 for s in file.services
                if s.id not in table or table[s.id].model_dump(exclude={"monthly_cost", "is_active", "renewal_day",
                                                                        "renewal_date", "notes", "billing_period"})
                != s.model_dump(exclude={"monthly_cost", "is_active", "renewal_day", "renewal_date", "notes",
                                         "billing_period"}))
        if self._differing or self._differing_services:
            since = self._import_info.get("imported_at")
            date = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d") if since else "the import"
            parts = []
            if self._differing:
                n = len(self._differing)
                parts.append(f"{n} key{'s' if n != 1 else ''} differ: {', '.join(self._differing)}")
            if self._differing_services:
                n = self._differing_services
                parts.append(f"{n} service{'s' if n != 1 else ''} differ{'s' if n == 1 else ''} from the Services tab")
            _log.info(f"config.yaml is ignored for settings (managed on the Settings page since {date}); "
                      + "; ".join(parts))

    # --- validating and saving -----------------------------------------------------------------------------------------

    async def validate(self, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                       secrets: Optional[Mapping[str, str]] = None) -> Validation:
        """Checks a change against the current state as a whole candidate config; nothing is applied."""
        return await validate_change(self, values, lists, secrets)

    async def apply(self, base_revision: int, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                    secrets: Optional[Mapping[str, str]] = None, source: str = "save") -> Applied:
        """Validates, writes the secrets, commits and goes live. Raises SettingsInvalid, SettingsConflict or
        SettingsWriteFailed (nothing saved for any of them). Each save is counted by its source and outcome."""
        outcome = "saved"
        try:
            return await self._apply(base_revision, values, lists, secrets or {}, source)
        except SettingsInvalid:
            outcome = "invalid"
            raise
        except SettingsConflict:
            outcome = "conflict"
            raise
        except SettingsWriteFailed:
            outcome = "failed"
            raise
        finally:
            telemetry.SETTINGS_SAVES.add(1, {"source": source if source in SOURCES else "unknown",
                                             "outcome": outcome})

    async def _apply(self, base_revision: int, values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                     secrets: Mapping[str, str], source: str) -> Applied:
        if source not in SOURCES:
            raise SettingsInvalid([{"where": "source", "message": f"Must be one of {', '.join(SOURCES)}"}], 400)
        async with self._lock:
            checked = await self.validate(values, lists, secrets)
            if not checked.ok:
                raise SettingsInvalid(checked.errors, checked.status,
                                      "Set by the environment" if checked.status == 400 else "Some values were refused")
            current, conflicts = await self.repository.settings_conflicts(base_revision, values, lists,
                                                                          checked.slots)
            if conflicts:
                raise SettingsConflict(current, conflicts)
            previous = None
            if checked.secret_updates or checked.secret_removals:
                previous = self.secrets.snapshot()
                try:
                    self.secrets.change(checked.secret_updates, checked.secret_removals)
                except SecretsFileUnusable:  # logged by the store, with what to do
                    raise SettingsWriteFailed(SECRETS_FILE_DAMAGED) from None
                except OSError as e:
                    _log.error(f"secrets.json could not be written ({type(e).__name__}); the settings were not saved")
                    raise SettingsWriteFailed() from None
            document = checked.document
            change_values = {k: document.values.get(k) for k in values}
            change_lists = {n: document.lists.get(n) for n in lists}
            try:
                revision, _, _, summary = await self.repository.save_settings(
                    base_revision, change_values, change_lists, source,
                    lambda before, after: checked.summary, slots=checked.slots)
            except BaseException as e:
                if previous is not None:
                    self._restore_secrets(previous)
                if isinstance(e, SettingsConflict) or not isinstance(e, Exception):
                    raise
                _log.error(f"The settings could not be written to the database ({type(e).__name__}); nothing was "
                           "saved")
                raise SettingsWriteFailed("The settings could not be saved; nothing was changed") from None
            return await self._go_live(revision, summary, checked.slots)

    def _restore_secrets(self, previous: dict) -> None:
        try:
            self.secrets.restore(previous)
            _log.warning("The database refused the save, so secrets.json was put back as it was")
        except OSError as e:
            _log.error(f"The database refused the save and secrets.json could not be put back ({type(e).__name__}); "
                       "check the slots on the Settings tab")

    async def _go_live(self, revision: int, summary: str, slots: Set[str]) -> Applied:
        old_document = self.document
        old_values = self.values()
        old_hooks = jsonable(list(self._effective.webhooks))
        _, stored = await self.repository.load_settings()
        self.document = self._known(stored)
        self.revision = revision
        config, dropped = self.build()
        if dropped:
            _log.warning(f"Settings left at their defaults: {', '.join(lenient.removed_paths(dropped))}")
        copy_into(self.config, config)  # one synchronous block: no request sees half of it
        self._effective = config
        new_values = self.values()
        changed = [k for k in new_values if new_values[k] != old_values.get(k)]
        live_keys = [k for k in changed if registry.get(k).live]
        changed_lists = ["webhooks"] if (jsonable(list(config.webhooks)) != old_hooks
                                         or old_document.lists.get("webhooks") != self.document.lists.get("webhooks")) \
            else []
        change = Change.of(keys=live_keys, lists=changed_lists, slots=slots, old=old_document, new=self.document)
        failed: List[str] = []
        if not change.empty:
            for name in await self.hub.publish(change):
                failed += self.hub.touched_by(name, change)
        self._live_failed = (self._live_failed - set(live_keys) - set(changed_lists) - set(slots)) | set(failed)
        restart = sorted(k for k in changed if not registry.get(k).live)
        _log.info(f"Settings saved as revision {revision}: {summary}")
        return Applied(revision=revision, summary=summary,
                       applied=sorted(changed) + sorted(changed_lists) + sorted(slots),
                       restart_needed=restart, live_failed=sorted(set(failed)),
                       reload="household.members" in changed)

    # --- versions, export, import --------------------------------------------------------------------------------------

    async def versions(self) -> List[Dict[str, Any]]:
        return await self.repository.settings_versions()

    async def version(self, revision: int) -> Optional[Dict[str, Any]]:
        """A kept version as a draft against the current document: every value it sets, null for a value stored now
        and not then, and the list (null: back to its default). Pinned keys are left out. Saving it restores it."""
        document = await self.repository.settings_version(revision)
        if document is None:
            return None
        values: Dict[str, Any] = {k: None for k in self.document.values}
        values.update({k: v for k, v in document.values.items() if k in registry.BY_KEY})
        for key in self.pinned():
            values.pop(key, None)
        lists = {name: document.lists.get(name) for name in registry.LISTS}
        return {"revision": revision, "values": values, "lists": lists}

    def export_yaml(self) -> str:
        """The stored values (those marked for export) and the set list, in config.yaml syntax. No secret: a webhook
        item has no URL and no stored signing secret (those are slots)."""
        data: dict = {}
        for key, value in self.document.values.items():
            setting = registry.BY_KEY.get(key)
            if setting is not None and setting.export:
                set_path(data, key, copy.deepcopy(value))
        for name, spec in registry.LISTS.items():
            items = self.document.lists.get(name)
            if items is not None:
                fields = set(spec.item_model.model_fields)
                set_path(data, spec.path, [{k: v for k, v in item.items() if k in fields} for item in items])
        body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if data else "{}\n"
        return EXPORT_HEADER + body

    def import_yaml(self, text: str) -> Dict[str, Any]:
        """An export (or a whole config.yaml) read into a draft {values, lists, notes, kept}. Nothing is applied and no
        secret comes in. Raises ValueError with a plain message."""
        try:
            raw = yamlsafe.load_plain(text, "settings file")
            if not isinstance(raw, dict):
                raise ValueError("A settings file is a YAML mapping (as Export writes it)")
        except ValueError:
            telemetry.SETTINGS_IMPORTS.add(1, {"kind": "yaml", "outcome": "invalid"})
            raise
        telemetry.SETTINGS_IMPORTS.add(1, {"kind": "yaml", "outcome": "draft"})
        result = read_settings(raw)
        notes = list(result.notes)
        kept = []
        for r in result.report:
            spec = registry.BY_KEY.get(r["key"])
            if spec is None:
                notes.append(f"{r['key']}: {plain(r['reason'])} (not imported)")
                continue
            kept.append(spec.key)
            notes.append(f"{spec.label}: {plain(r['reason'])}. Left as it is now.")
        pinned = self.pinned()
        values = {}
        for key, value in result.document.values.items():
            if key in pinned:
                notes.append(f"{registry.get(key).label}: set by the environment variable {registry.get(key).env} "
                             "(not imported)")
            else:
                values[key] = value
        return {"values": values, "lists": dict(result.document.lists), "notes": notes, "kept": kept}
