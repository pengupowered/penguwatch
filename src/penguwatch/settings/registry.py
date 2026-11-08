"""The registry: the one list of what the Settings tab shows, in its order, with the words it uses.

A `Setting` names a dotted key of the settings document, which is its config.yaml path
("optimizer.max_active_services"). A driver's options are keyed under the driver's name ("tvmaze.timeout_seconds"),
as config.yaml writes them, and resolve to the driver's `options_model`; the driver itself says which of them the page
shows and in which words (its `settings`, drivers.base.DriverSetting), so nothing here names a service. Type, range,
choices and default are *not* repeated here: they are read from the pydantic field the key names (`Setting.field()`,
`.default()`, `.constraints()`), so a range exists in one place, the section model.

Also here: the sections (`SECTIONS`), the one list (`LISTS`: webhooks), the secret slots (`SECRET_SLOTS`) and the
fields of the stored sections that are deliberately not on the page (`NOT_ON_PAGE`). tests/test_settings_registry.py
fails when a section model gains a field that is in none of them.
"""

import dataclasses
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from penguwatch.config import AppConfig, WebhookItem
from penguwatch.drivers import DRIVERS
from penguwatch.settings import env as settings_env


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    order: int


SECTIONS: Tuple[Section, ...] = tuple(Section(id, title, n) for n, (id, title) in enumerate((
    ("household", "Household"),
    ("sync", "Sync"),
    ("optimizer", "Optimizer"),
    ("notifications", "Notifications"),
    ("display", "Display"),
    ("integrations", "Integrations"),
    ("sources", "Data sources"),
    ("advanced", "Advanced"),
), start=1))
SECTION_IDS = tuple(s.id for s in SECTIONS)

# AppConfig fields that are not sections of their own: `driver_sections` holds each driver's options, keyed in the
# document by the driver's name (resolved through DRIVERS[name].options_model).
_DRIVER_SECTIONS = "driver_sections"


def driver_option_models() -> Dict[str, Type[BaseModel]]:
    """{driver name: its options model} for the drivers that have options."""
    return {name: driver.options_model for name, driver in sorted(DRIVERS.items())
            if getattr(driver, "options_model", None) is not None}


def _top(root: Type[BaseModel], part: str) -> Optional[Type[BaseModel]]:
    """The model a key's first part names: a section of `root`, or a driver's options model."""
    info = root.model_fields.get(part)
    if info is not None and part != _DRIVER_SECTIONS:
        inner = unwrap_optional(info.annotation)[0]
        return inner if isinstance(inner, type) and issubclass(inner, BaseModel) else None
    return driver_option_models().get(part)


def _resolve(key: str) -> Tuple[Type[BaseModel], str, FieldInfo]:
    """(section model, field name, field) of a dotted key, walking AppConfig's nested models (a driver's name first
    resolves to its options model). KeyError when the key names no field."""
    parts = key.split(".")
    if len(parts) < 2:
        raise KeyError(key)
    model = _top(AppConfig, parts[0])
    if model is None:
        raise KeyError(key)
    for part in parts[1:-1]:
        info = model.model_fields.get(part)
        inner = unwrap_optional(info.annotation)[0] if info is not None else None
        if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
            raise KeyError(key)
        model = inner
    info = model.model_fields.get(parts[-1])
    if info is None:
        raise KeyError(key)
    return model, parts[-1], info


def unwrap_optional(annotation) -> Tuple[Any, bool]:
    """(the annotation without Optional, whether None is allowed)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return (args[0] if len(args) == 1 else annotation), len(args) < len(typing.get_args(annotation))
    return annotation, False


def field_constraints(info: FieldInfo) -> Dict[str, Any]:
    """What the page needs to know of a field: {type, nullable, min, max, exclusive_min, exclusive_max, min_length,
    max_length, step, choices}. type is number | integer | boolean | text | choice, or the field's
    json_schema_extra["ui_type"] (timezone | theme | names | hour | driver | drivers), or list | object | map for
    list-item fields. Read from the annotation and its pydantic metadata."""
    annotation, nullable = unwrap_optional(info.annotation)
    extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
    origin = typing.get_origin(annotation)
    choices: Optional[List[Any]] = None
    if extra.get("ui_type"):
        kind = extra["ui_type"]
    elif origin is Literal:
        kind, choices = "choice", list(typing.get_args(annotation))
    elif annotation is bool:
        kind = "boolean"
    elif annotation is int:
        kind = "integer"
    elif annotation is float:
        kind = "number"
    elif annotation is str:
        kind = "text"
    elif origin in (list, List):
        kind = "list"
    elif origin in (dict, Dict):
        kind = "map"
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        kind = "object"
    else:
        kind = "text"
    integer = annotation is int
    out: Dict[str, Any] = {"type": kind, "nullable": nullable, "min": None, "max": None, "exclusive_min": False,
                           "exclusive_max": False, "min_length": None, "max_length": None,
                           "step": 1 if integer else None, "choices": choices}
    for meta in info.metadata:
        for attr, target, exclusive in (("ge", "min", False), ("gt", "min", True), ("le", "max", False),
                                        ("lt", "max", True)):
            value = getattr(meta, attr, None)
            if value is not None:
                out[target] = value
                out[f"exclusive_{target}"] = exclusive
        for attr in ("min_length", "max_length"):
            if getattr(meta, attr, None) is not None:
                out[attr] = getattr(meta, attr)
    return out


def field_default(info: FieldInfo) -> Any:
    """The field's default as JSON-ready data (a model default is dumped)."""
    value = info.get_default(call_default_factory=True)
    return value.model_dump() if isinstance(value, BaseModel) else value


@dataclass(frozen=True)
class Setting:
    key: str              # dotted config.yaml path: "optimizer.max_active_services", "tvmaze.timeout_seconds"
    section: str          # one of SECTION_IDS
    group: Optional[str]  # sub-heading in the section ("Daily digest"; a driver's options: the driver's label)
    label: str            # "Services kept at once"
    effect: str           # one line: what changing it does
    unit: Optional[str] = None   # shown after the input: "min", "h", "days", "s"
    live: bool = True     # False: applies at the next start; the live config keeps the running value
    env: Optional[str] = None    # the variable that overrides it (penguwatch.settings.env.SETTING_ENV)
    advanced: bool = False       # set from the section (advanced)
    export: bool = True          # in Settings > Export
    order: int = 0        # place within its section (set from the registry's order, from 1)
    choice_labels: Optional[Dict[str, str]] = None  # stored choice -> the words the page shows ("ntfy": "ntfy topic")
    empty: Optional[str] = None  # what an empty value means, in words ("the server's zone (TZ, else UTC)")
    driver: Optional[str] = None  # a driver's option: the driver's name (the page may hide it while no role uses it)

    def resolve(self) -> Tuple[Type[BaseModel], str, FieldInfo]:
        """(section model, field name, field)."""
        return _resolve(self.key)

    def field(self) -> FieldInfo:
        return _resolve(self.key)[2]

    def default(self) -> Any:
        return field_default(self.field())

    def constraints(self) -> Dict[str, Any]:
        return field_constraints(self.field())


@dataclass(frozen=True)
class ListSpec:
    """A list of the settings document. `path` is its place in AppConfig / config.yaml; `item_model` validates one
    stored item; `default` gives the items of a list nobody set; `max_items` caps it."""
    name: str
    path: str
    section: str
    label: str
    item_model: Type[BaseModel]
    default: Callable[[], List[BaseModel]]
    max_items: int


@dataclass(frozen=True)
class SecretSlot:
    """A named place in secrets.json (next to the database), written through the Settings tab and never read back.
    A `pattern` slot stands for one per item: "webhook.{id}.url" and "webhook.{id}.secret" per webhook."""
    slot: str
    label: str
    kind: Literal["url", "token"]
    section: str
    env: Optional[str] = None
    pattern: bool = False


def _entries(section: str, rows: List[Setting]) -> List[Setting]:
    return [dataclasses.replace(s, section=section, order=n, advanced=section == "advanced")
            for n, s in enumerate(rows, start=1)]


def _s(key: str, label: str, effect: str, group: Optional[str] = None, **kw) -> Setting:
    return Setting(key=key, section="", group=group, label=label, effect=effect, **kw)


_RESTART = dict(live=False)


def _driver_rows(section: str) -> List[Setting]:
    """The options the drivers show in `section` (each driver's `settings`), by driver name, grouped under its
    label."""
    return [Setting(key=f"{name}.{row.field}", section="", group=driver.label, label=row.label, effect=row.effect,
                    unit=row.unit, live=row.live, driver=name)
            for name, driver in sorted(DRIVERS.items()) for row in getattr(driver, "settings", ())
            if row.section == section]

SETTINGS: Tuple[Setting, ...] = tuple(
    _entries("household", [
        _s("household.members", "Who watches",
           "One or two names. Two add \"Both\" and the \"who wants to watch\" control on every title."),
    ])
    + _entries("sync", _driver_rows("sync"))
    + _entries("optimizer", [
        _s("optimizer.max_active_services", "Services kept at once",
           "How many streaming services the plan keeps active at the same time."),
        _s("optimizer.household_priority_boost", "Weight of titles both want",
           "How much a title everyone wants to watch pulls its service up the plan."),
        _s("optimizer.ready_to_binge_boost", "Weight of complete seasons",
           "How much a title whose season has finished airing pulls its service up the plan."),
    ])
    + _entries("notifications", [
        _s("notify.kind", "Send the digest as", "An ntfy topic gets a push message; a webhook gets a JSON POST.",
           "Daily digest", env="PENGUWATCH_NOTIFY_KIND",
           choice_labels={"ntfy": "ntfy topic", "webhook": "JSON webhook"}),
        _s("notify.hour", "Check every day at", "The local hour (Display > Time zone) of the daily check.",
           "Daily digest", env="PENGUWATCH_NOTIFY_HOUR"),
        _s("notify.renewal_warning_days", "Warn before a renewal",
           "A paid service that renews this many days away or sooner, and is unused or will be finished, is in the "
           "digest.", "Daily digest", unit="days"),
    ])
    + _entries("display", [
        _s("display.theme", "Theme for new browsers",
           "What a browser shows until its person picks a theme in the header menu."),
        _s("server.timezone", "Time zone",
           "The household's day: renewal countdowns, what airs today, the digest hour, History's charts.",
           env="PENGUWATCH_TZ", empty="the server's zone (TZ, else UTC)"),
    ])
    + _entries("integrations", [
        _s("mcp.enabled", "Allow AI assistants to read (MCP)",
           "Serves the read-only MCP endpoint at /mcp. Guarded like the rest of the app: with no access token, anyone "
           "who can reach the port can use it.", env="PENGUWATCH_MCP_ENABLED"),
        _s("display.grafana_url", "Grafana address",
           "The page header links to PenguWatch's dashboards in this Grafana.", env="PENGUWATCH_GRAFANA_URL",
           empty="no Grafana links"),
    ])
    + _entries("sources", [
        _s("drivers.library", "Library", "Where your library lives: an account on a tracker, or only here (Local).",
           **_RESTART),
        _s("drivers.catalog", "Catalogue", "Where titles are searched and their details come from, tried in order.",
           **_RESTART),
        _s("drivers.schedule", "Schedule", "Where upcoming episodes come from, tried in order; none turns it off.",
           **_RESTART),
        _s("drivers.discovery", "Discovery", "Where trending titles and recommendations come from; none turns it "
           "off.", **_RESTART),
    ])
    + _entries("advanced", [
        _s("optimizer.value_window_days", "Viewing window",
           "The days of viewing the optimizer looks at for \"used\" and the cost per hour.", "Optimizer",
           unit="days"),
        _s("optimizer.unused_hours", "Not used below",
           "A service watched less than this in the viewing window counts as not used.", "Optimizer", unit="h"),
        _s("history.bulk_min", "Bulk mark from",
           "Episodes sharing one exact watched time from this many on count as marked in bulk, not watched then.",
           "History", unit="episodes"),
        _s("server.log_level", "Log level", "How much PenguWatch writes to its log.", "Logging",
           env="PENGUWATCH_LOG_LEVEL", choice_labels={"debug": "Everything (for debugging)", "info": "Normal",
                                                      "warning": "Warnings and errors", "error": "Errors only"}),
    ] + _driver_rows("advanced"))
)

BY_KEY: Dict[str, Setting] = {s.key: s for s in SETTINGS}

MAX_WEBHOOKS = 10

LISTS: Dict[str, ListSpec] = {spec.name: spec for spec in (
    ListSpec("webhooks", "webhooks", "notifications", "Event webhooks", WebhookItem, list, MAX_WEBHOOKS),
)}

SECRET_SLOTS: Tuple[SecretSlot, ...] = (
    SecretSlot("notify.url", "Digest address", "url", "notifications", env=settings_env.SECRET_ENV["notify.url"]),
    SecretSlot("webhook.{id}.url", "Webhook address", "url", "notifications", pattern=True),
    SecretSlot("webhook.{id}.secret", "Webhook signing secret", "token", "notifications", pattern=True),
)

# Fields of AppConfig (and of the drivers' options) the page does not show, and why. Everything else is a Setting or
# one of LISTS.
NOT_ON_PAGE: Dict[str, str] = {
    "server.host": "bootstrap: the listen address is needed before the database opens (PENGUWATCH_HOST)",
    "server.port": "bootstrap: needed before the database opens (PENGUWATCH_PORT)",
    "database.db_path": "bootstrap: where the settings themselves are stored (PENGUWATCH_DB_PATH)",
    "services": "owned by the services table and the Services & Sync tab",
    "ratings.sources": "owned by the rating_sources table and the Ratings tab",
    # and each driver's `not_on_page` options (its credentials), keyed "<driver>.<option>"
    **{f"{name}.{option}": why for name, driver in sorted(DRIVERS.items())
       for option, why in getattr(driver, "not_on_page", {}).items()},
}


def get(key: str) -> Setting:
    """The Setting for `key`; KeyError for a key the page does not have."""
    return BY_KEY[key]


def in_section(section: str) -> List[Setting]:
    """The settings a section renders, in order."""
    return [s for s in SETTINGS if s.section == section]


def list_spec_for_path(path: str) -> Optional[ListSpec]:
    return next((spec for spec in LISTS.values() if spec.path == path), None)


def slot_kind(slot: str) -> Optional[SecretSlot]:
    """The SecretSlot a concrete slot name belongs to ("webhook.home.url" -> the "webhook.{id}.url" pattern), or None
    for a name that is no slot."""
    for spec in SECRET_SLOTS:
        if not spec.pattern:
            if spec.slot == slot:
                return spec
            continue
        head, tail = spec.slot.split("{id}")
        if slot.startswith(head) and slot.endswith(tail) and len(slot) > len(head) + len(tail):
            from penguwatch.config import WEBHOOK_ID_RE
            if WEBHOOK_ID_RE.fullmatch(slot[len(head):-len(tail)]):
                return spec
    return None


def stored_leaf_paths(root: Type[BaseModel] = AppConfig) -> List[str]:
    """Every leaf field path of AppConfig, descending into nested section models and into each driver's options
    (under the driver's name); a list (of items) is one leaf."""
    found: List[str] = []

    def walk(model: Type[BaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            if not prefix and name == _DRIVER_SECTIONS:
                for driver, options in driver_option_models().items():
                    walk(options, f"{driver}.")
                continue
            inner = unwrap_optional(info.annotation)[0]
            path = f"{prefix}{name}"
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                walk(inner, path + ".")
            else:
                found.append(path)

    walk(root, "")
    return found


def unplaced_fields(root: Type[BaseModel] = AppConfig) -> List[str]:
    """Leaf fields that are neither a Setting, nor one of LISTS, nor in NOT_ON_PAGE: a field someone added to a
    section model without deciding whether the page shows it. Empty for this build (a test pins it)."""
    return [path for path in stored_leaf_paths(root)
            if path not in BY_KEY and path not in NOT_ON_PAGE and list_spec_for_path(path) is None]
