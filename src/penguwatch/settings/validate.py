"""Validating a settings change (SettingsManager.validate): the candidate config built from the stored document and the
change, checked as a whole (the section models, the webhooks, the secret slots), with each error placed where the page
shows it, the warnings, and the change's version line (`describe`).

Nothing here writes: `validate` reads the manager's current document, live config, environment and secret slots, and
returns a `Validation` that SettingsManager.apply commits. No message repeats a submitted value.
"""

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

from pydantic import ValidationError

from penguwatch.config import AppConfig, WebhookItem, member_number, normal_members, webhook_id
from penguwatch.secrets import slot_path
from penguwatch.settings import lenient, registry
from penguwatch.settings.document import SettingsDocument
from penguwatch.settings.importer import present
from penguwatch.settings.plain import plain
from penguwatch.webhooks import check_hook_url, check_item_secrets, check_save, check_slot_secret

if TYPE_CHECKING:
    from penguwatch.settings.manager import SettingsManager

SECRET_MAX_CHARS = 4096
# The webhook rules of a save are penguwatch.webhooks' (one place, also used at delivery):
#   check_item_secrets   a new or changed secret_env / secret_file is refused (the page can only keep an imported
#                        value or clear it); items are paired with the stored item BY ID only
#   check_save           a hook that still signs with an imported secret_env / secret_file can't get a new address
#   check_hook_url       every webhook.<id>.url AND notify.url write: http(s), no credentials, not this machine or a
#                        link-local address (the SSRF denylist at save time; GuardedTransport repeats it at connect time)
#   check_slot_secret    every webhook.<id>.secret write: at least 16 characters, stripped


@dataclass
class Validation:
    errors: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    summary: str = ""
    document: Optional[SettingsDocument] = None  # the document after the change
    secret_updates: Dict[str, Any] = field(default_factory=dict)
    secret_removals: List[Tuple[str, ...]] = field(default_factory=list)
    slots: Set[str] = field(default_factory=set)  # slots written or cleared
    status: int = 422

    @property
    def ok(self) -> bool:
        return not self.errors


# --- the version line of a change ---------------------------------------------------------------------------------

def _shown(setting: registry.Setting, value: Any) -> str:
    """A value as the page shows it, for the version line."""
    if value is None or value == "":
        return setting.empty or "none"
    if setting.choice_labels and isinstance(value, str):
        return setting.choice_labels.get(value, value)
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        text = f"{round(value, 4):g}"
        return f"{text} {setting.unit}" if setting.unit else text
    if isinstance(value, list):
        # the household's people are {id, name}: shown by name
        return ", ".join(str(v.get("name", v)) if isinstance(v, dict) else str(v) for v in value) or "none"
    text = str(value)
    return text if len(text) <= 40 else text[:39] + "…"


def list_item_id(item: dict) -> str:
    """A list item's id ("" when it has none)."""
    return str(item.get("id") or "")


def _describe_list(name: str, old: Optional[List[dict]], new: Optional[List[dict]]) -> Optional[str]:
    label = registry.LISTS[name].label
    if new is None:
        return None if old is None else f"{label} back to the default"
    before = {list_item_id(i): i for i in (old or [])}
    after = {list_item_id(i): i for i in new}
    added = [i for i in after if i not in before]
    removed = [i for i in before if i not in after]
    changed = [i for i in after if i in before and before[i] != after[i]]
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if removed:
        parts.append(f"{len(removed)} removed")
    if changed:
        parts.append(f"{len(changed)} changed")
    if not parts and [i for i in after if i in before] != [i for i in before if i in after]:
        parts.append("order changed")
    if not parts and old is None:
        parts.append("set" if new else "set to none")
    return f"{label}: {', '.join(parts)}" if parts else None


def _slot_words(slots: Iterable[str]) -> List[str]:
    """The secret slots of a change, by their labels (never a value, never a webhook's address)."""
    words = []
    for slot in sorted(slots):
        spec = registry.slot_kind(slot)
        label = spec.label if spec else "secret"
        hook = slot[len("webhook."):].rsplit(".", 1)[0] if spec is not None and spec.pattern else None
        words.append(f"{label} ({hook}) changed" if hook else f"{label} changed")
    return words


def describe(old: SettingsDocument, new: SettingsDocument, slots: Iterable[str] = ()) -> str:
    """The version line of a change: "Services kept at once 3 → 2; Event webhooks: 1 added; Digest address changed"."""
    parts: List[str] = []
    for setting in registry.SETTINGS:
        before, after = old.values.get(setting.key), new.values.get(setting.key)
        if (setting.key in old.values) == (setting.key in new.values) and before == after:
            continue
        if setting.key not in new.values:
            parts.append(f"{setting.label} reset")
            continue
        was = _shown(setting, before if setting.key in old.values else setting.default())
        now = _shown(setting, after)
        parts.append(f"{setting.label} {was} → {now}" if was != now else f"{setting.label} {now}")
    for name in registry.LISTS:
        text = _describe_list(name, old.lists.get(name), new.lists.get(name))
        if text:
            parts.append(text)
    parts += _slot_words(slots)
    if not parts:
        return "No changes"
    text = "; ".join(parts)
    return text[0].upper() + text[1:]


# --- where an error of the candidate config is shown ----------------------------------------------------------------

def _where(loc: Tuple[Any, ...], data: dict, error: dict, touched_values: List[str], document: SettingsDocument,
           hook_ids: List[str]) -> List[str]:
    text = lenient.path_text(tuple(loc))
    if loc and loc[0] == "webhooks":
        if len(loc) > 1 and isinstance(loc[1], int):
            index = loc[1]
            rest = ".".join(str(p) for p in loc[2:])
            sent = data.get("webhooks") or []
            hook = list_item_id(sent[index]) if index < len(sent) else ""
            if rest == "url":
                return [f"secrets.webhook.{hook}.url"]
            ids = [list_item_id(i) for i in document.lists.get("webhooks") or []]
            index = ids.index(hook) if hook in ids else index
            return [f"lists.webhooks[{index}]" + (f".{rest}" if rest else "")]
        return ["lists.webhooks"]
    first = loc[0] if loc else ""
    if first == "driver_sections" and len(loc) > 1:  # a driver's options: keyed by the driver's name
        loc = loc[1:]
        text = lenient.path_text(tuple(loc))
    for key in registry.BY_KEY:
        if text == key or text.startswith(key + "."):
            return [f"values.{key}"]
    # a rule across values (the quiet interval not below the active one): the touched values whose removal clears it
    candidates = [tuple(k.split(".")) for k in touched_values if present(data, k)]
    found = lenient.culprits(AppConfig, data, error, tuple(loc), candidates)
    out = [f"values.{'.'.join(path)}" for path in found]
    return out or [f"values.{text}" if text else "values"]


# --- the secret slots -------------------------------------------------------------------------------------------------

def _slot_hook(slot: str) -> Optional[Tuple[str, str]]:
    """(webhook id, "url" | "secret") of a webhook slot."""
    if not slot.startswith("webhook."):
        return None
    hook, _, part = slot[len("webhook."):].rpartition(".")
    return hook, part


def _check_secrets(manager: "SettingsManager", secrets: Mapping[str, str], document: SettingsDocument,
                   out: Validation) -> None:
    items = {list_item_id(i): i for i in document.lists.get("webhooks") or []}
    for slot, value in secrets.items():
        where = f"secrets.{slot}"
        path = slot_path(slot)
        if path is None:
            out.errors.append({"where": where, "message": "No such secret"})
            continue
        variable = manager.secret_env(slot)
        if variable:
            out.errors.append({"where": where, "message": f"{variable} sets this; remove it and restart to edit it "
                                                          "here."})
            out.status = 400
            continue
        hook = _slot_hook(slot)
        if hook is not None and hook[1] == "secret" and hook[0] in items:
            item = items[hook[0]]
            if item.get("secret_env"):
                out.errors.append({"where": where, "message": f"The webhook reads its signing secret from "
                                                              f"{item['secret_env']}; change it there."})
                out.status = 400
                continue
            if item.get("secret_file"):
                out.errors.append({"where": where, "message": "The webhook reads its signing secret from a file."})
                out.status = 400
                continue
        if not isinstance(value, str):
            out.errors.append({"where": where, "message": "Must be text"})
            continue
        value = value.strip()
        if hook is not None and hook[0] not in items and value:
            out.errors.append({"where": where, "message": "No webhook has this id"})
            continue
        if value:
            if len(value) > SECRET_MAX_CHARS or "\n" in value or "\r" in value:
                out.errors.append({"where": where, "message": f"Must be one line of at most {SECRET_MAX_CHARS} "
                                                              "characters"})
                continue
            problem = check_hook_url(value) if slot.endswith(".url") else check_slot_secret(value)
            if problem:
                out.errors.append({"where": where, "message": problem})
                continue
            node = out.secret_updates
            for part in path[:-1]:
                node = node.setdefault(part, {})
            node[path[-1]] = value
        else:
            out.secret_removals.append(path)
        out.slots.add(slot)


def _check_webhooks(manager: "SettingsManager", items: List[dict], pending: Mapping[str, str],
                    out: Validation) -> None:
    stored = {list_item_id(i): i for i in manager.document.lists.get("webhooks") or []}
    ids: Set[str] = set()
    names: Set[str] = set()
    if len(items) > registry.MAX_WEBHOOKS:
        out.errors.append({"where": "lists.webhooks", "message": f"At most {registry.MAX_WEBHOOKS} webhooks"})
    for i, item in enumerate(items):
        where = f"lists.webhooks[{i}]"
        hook = list_item_id(item)
        if hook in ids:
            out.errors.append({"where": f"{where}.id", "message": "This id is used twice"})
        ids.add(hook)
        name = item.get("name")
        if not name:
            out.errors.append({"where": f"{where}.name", "message": "Give the webhook a name"})
        elif name in names:
            out.errors.append({"where": f"{where}.name", "message": "This name is used twice"})
        names.add(name)
        before = stored.get(hook)  # by id only, never by name: None for an id that is not stored
        for name, message in check_item_secrets(item, before).items():
            out.errors.append({"where": f"{where}.{name}", "message": message})
        url_slot = f"webhook.{hook}.url"
        if hook not in stored and not pending.get(url_slot) and not any(
                e["where"] == f"secrets.{url_slot}" for e in out.errors):
            out.errors.append({"where": f"secrets.{url_slot}", "message": "A new webhook needs its address"})


def _check_moves(manager: "SettingsManager", document: SettingsDocument, pending: Mapping[str, str],
                 out: Validation) -> None:
    """H1, on every save (also one that sends no list, only a `webhook.<id>.url` slot): a hook that still signs with an
    imported secret_env / secret_file may not get a new address. Each item of the document after the save is paired
    with the stored item of the same id (never by name)."""
    stored = {list_item_id(i): i for i in manager.document.lists.get("webhooks") or []}
    for item in document.lists.get("webhooks") or []:
        hook = list_item_id(item)
        url_slot = f"webhook.{hook}.url"
        new_url = pending.get(url_slot)
        url_changed = bool(new_url) and new_url != manager.stored_secret(url_slot)
        refused = check_save(stored.get(hook), item, url_changed, bool(pending.get(f"webhook.{hook}.secret")))
        if refused and not any(e["where"] == f"secrets.{url_slot}" for e in out.errors):
            out.errors.append({"where": f"secrets.{url_slot}", "message": refused})


def _normalize_webhooks(items: List[dict], stored: List[dict], out: Validation) -> List[dict]:
    """New webhooks get an id from their name (an item saved before keeps its id); an item never carries an address
    (that is the slot webhook.<id>.url)."""
    taken = {list_item_id(i) for i in items if list_item_id(i)} | {list_item_id(i) for i in stored}
    normalized = []
    for number, item in enumerate(items, start=1):
        item = dict(item)
        if "url" in item:
            out.errors.append({"where": f"lists.webhooks[{number - 1}].url",
                               "message": "The address is a secret: send it as secrets.webhook.<id>.url"})
            item.pop("url")
        if not list_item_id(item):
            item["id"] = webhook_id(item.get("name") or f"webhook-{number}", taken)
            taken.add(item["id"])
        try:
            item = WebhookItem.model_validate(item).model_dump(mode="json")
        except ValidationError as e:
            for error in e.errors(include_url=False, include_input=False, include_context=False):
                rest = ".".join(str(p) for p in error.get("loc", ()))
                out.errors.append({"where": f"lists.webhooks[{number - 1}]" + (f".{rest}" if rest else ""),
                                   "message": plain(lenient.reason(error))})
        normalized.append(item)
    return normalized


def member_ids(value: Any, current: Iterable[str], floor: int) -> Any:
    """The household members a save sends, with their ids: a person who is a member now keeps their id (a rename keeps
    every title with them); anyone else is new and gets the next unused one, above `floor` (the highest id given so
    far, so an id is never reused). The old shape (a list of names) maps to person1, person2, ... in order first."""
    if not isinstance(value, list):
        return value
    if value and all(isinstance(i, str) for i in value):
        value = normal_members(value)
    current = set(current)
    floor = max([floor, *(member_number(i) for i in current)])
    items: List[Any] = []
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        elif isinstance(item, dict):
            item = {k: item[k] for k in ("id", "name") if k in item}
            if item.get("id") not in current:
                item.pop("id", None)
        items.append(item)
    return normal_members(items, floor)


def _names(members: List[Any], ids: Iterable[str]) -> str:
    """"Ann", "Ann or Bob", "Ann, Bob or Cid": the names of `ids`."""
    by_id = {m.get("id"): str(m.get("name")) for m in members if isinstance(m, dict)}
    names = [by_id.get(i, i) for i in ids]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


async def _warnings(manager: "SettingsManager", values: Mapping[str, Any], document: SettingsDocument,
                    out: Validation) -> None:
    def value(key):
        return document.values.get(key, registry.get(key).default())

    if "household.members" in values:
        before = manager.effective_value("household.members")
        after = value("household.members")
        if isinstance(before, list) and isinstance(after, list):
            kept = [m.get("id") for m in after if isinstance(m, dict)]
            removed = [m.get("id") for m in before if isinstance(m, dict) and m.get("id") not in kept]
            tagged, alone = await manager.repository.household_removal(removed, kept)
            if tagged and kept:
                first = next((str(m.get("name")) for m in after if isinstance(m, dict)), "")
                who = _names(before, removed)
                text = (f"{tagged} title{'s are' if tagged != 1 else ' is'} for {who}; saving takes "
                        f"{'them' if len(removed) != 1 else 'that person'} off {'it' if tagged == 1 else 'them'}.")
                if alone == tagged == 1:
                    text += f" It is for no one else and moves to {first}."
                elif alone:
                    text += (f" {alone} of them {'are' if alone != 1 else 'is'} for no one else and "
                             f"move{'' if alone != 1 else 's'} to {first}.")
                out.warnings.append({"where": "values.household.members", "message": text})
    if "mcp.enabled" in values and value("mcp.enabled") is True and not manager.token_set():
        out.warnings.append({"where": "values.mcp.enabled", "message":
                             "No access token is set: anyone who can reach this port can use the MCP endpoint."})


async def validate(manager: "SettingsManager", values: Mapping[str, Any], lists: Mapping[str, Optional[List[dict]]],
                   secrets: Optional[Mapping[str, str]] = None) -> Validation:
    """Checks a change against the manager's current state as a whole candidate config; nothing is applied."""
    secrets = dict(secrets or {})
    out = Validation()
    pinned = manager.pinned()
    new_values = dict(manager.document.values)
    for key, value in values.items():
        if key not in registry.BY_KEY:
            out.errors.append({"where": f"values.{key}", "message": "No such setting"})
            continue
        if key in pinned:
            out.errors.append({"where": f"values.{key}", "message": f"{registry.get(key).env} sets this; remove it "
                                                                    "and restart to edit it here."})
            out.status = 400
            continue
        if value is None:
            new_values.pop(key, None)
        elif key == "household.members":
            current = [m.get("id") for m in manager.effective_value(key) or [] if isinstance(m, dict)]
            new_values[key] = member_ids(copy.deepcopy(value), current,
                                         await manager.repository.last_member_number())
        else:
            new_values[key] = copy.deepcopy(value)
    new_lists = dict(manager.document.lists)
    for name, items in lists.items():
        if name not in registry.LISTS:
            out.errors.append({"where": f"lists.{name}", "message": "No such list"})
            continue
        if items is not None and not (isinstance(items, list) and all(isinstance(i, dict) for i in items)):
            out.errors.append({"where": f"lists.{name}", "message": "Must be a list of items"})
            continue
        if items is not None:
            items = _normalize_webhooks(items, manager.document.lists.get(name) or [], out)
        new_lists[name] = items
    if out.errors and out.status == 400:
        return out
    document = SettingsDocument(values=new_values, lists={k: v for k, v in new_lists.items() if v is not None})
    _check_secrets(manager, secrets, document, out)
    refused = {e["where"][len("secrets."):] for e in out.errors if e["where"].startswith("secrets.")}
    pending = {slot: (v or "").strip() for slot, v in secrets.items() if isinstance(v, str) and slot not in refused}
    if "webhooks" in lists and lists["webhooks"] is not None:
        _check_webhooks(manager, document.lists.get("webhooks") or [], pending, out)
    _check_moves(manager, document, pending, out)
    if "display.theme" in values and values["display.theme"] is not None:
        from penguwatch.web import themes
        if values["display.theme"] not in themes.builtin_ids():
            out.errors.append({"where": "values.display.theme", "message": "Pick one of the built-in themes"})

    def secret(slot: str) -> Optional[str]:
        if slot in pending:
            return pending[slot] or None
        return manager.stored_secret(slot)

    data = manager.candidate(document, secret)
    try:
        AppConfig.model_validate(data)
    except ValidationError as e:
        hook_ids = [list_item_id(i) for i in document.lists.get("webhooks") or []]
        for error in e.errors(include_url=False, include_input=False, include_context=False):
            loc = tuple(error.get("loc", ()))
            scope = ".".join(str(p) for p in loc[:1] if isinstance(p, str))
            message = plain(lenient.reason(error), scope)
            for where in _where(loc, data, error, list(values), document, hook_ids):
                key = where.removeprefix("values.")
                if key in manager.refused_keys and key not in values:
                    continue  # a stored value a newer range refuses: its default runs until a save of that key
                if {"where": where, "message": message} not in out.errors:
                    out.errors.append({"where": where, "message": message})
    await _warnings(manager, values, document, out)
    # removing a webhook removes its address and its stored signing secret
    if "webhooks" in lists:
        kept = {list_item_id(i) for i in document.lists.get("webhooks") or []}
        for item in manager.document.lists.get("webhooks") or []:
            hook = list_item_id(item)
            if hook in kept:
                continue
            for part in ("url", "secret"):
                slot = f"webhook.{hook}.{part}"
                if manager.stored_secret(slot) is not None or slot in out.slots:
                    out.slots.add(slot)
            out.secret_removals.append(("webhooks", hook))
    out.document = document
    out.summary = describe(manager.document, document, out.slots)
    return out
