"""Configuration management for PenguWatch."""

import logging
import re
from urllib.parse import urlsplit
from importlib import resources
from pathlib import Path
import os
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from penguwatch.drivers import DEFAULT_CHAIN, DEFAULT_LIBRARY, DRIVERS, check_role
from penguwatch.events import EVENT_TYPES
from penguwatch.models import ServiceConfig
from penguwatch.ratings import RatingSourceSpec
from penguwatch.settings.general import check_timezone, normal_log_level
from penguwatch.web.themes import ID_RE

logger = logging.getLogger("penguwatch.config")

_GRAFANA_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")


def plain_base_url(value: str, name: str = "PENGUWATCH_GRAFANA_URL") -> str:
    """A Grafana base URL without the trailing slash; "" for none. Raises ValueError for anything that is not a plain
    http(s) base URL (no query, fragment or credentials)."""
    url = (value or "").strip().rstrip("/")
    if url and not _GRAFANA_URL_RE.fullmatch(url):
        raise ValueError(f"{name} must be a plain http(s) base URL such as https://grafana.example:3000")
    return url


# The section models below are also the Settings tab's ranges and defaults: penguwatch.settings.registry reads type,
# range, choices and default from these fields (a range lives in one place). A field added to a stored section must
# be placed in the registry, LISTS or NOT_ON_PAGE (tests/test_settings_registry.py fails otherwise).


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # "INFO" and "warn" are read as info and warning
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # IANA zone for household-day decisions; empty = TZ env var, else UTC
    timezone: str = Field(default="", json_schema_extra={"ui_type": "timezone"})

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level(cls, value):
        return normal_log_level(value)

    @field_validator("timezone", mode="before")
    @classmethod
    def _zone(cls, value):
        return check_timezone(value)


class DatabaseConfig(BaseModel):
    db_path: str = "data/penguwatch.db"


class DriversConfig(BaseModel):
    """Which driver fills each role (config.yaml `drivers:`; docs/drivers.md). One library driver; the catalog,
    schedule and discovery roles take a list, tried in order (the first that has the title wins); an empty schedule or
    discovery list turns that feature off, while the catalog needs at least one driver. Checked at start-up:
    an unknown driver, or one without the capability its role needs, stops the start with a message naming it."""

    model_config = ConfigDict(extra="forbid")

    # ui_type: the Settings tab shows `driver` as one pick and `drivers` as an ordered pick of the drivers that offer
    # the role's capability (GET /api/settings/schema `drivers` and `roles`)
    library: str = Field(default=DEFAULT_LIBRARY, json_schema_extra={"ui_type": "driver"})
    catalog: List[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN), json_schema_extra={"ui_type": "drivers"})
    schedule: List[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN), json_schema_extra={"ui_type": "drivers"})
    discovery: List[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN), json_schema_extra={"ui_type": "drivers"})

    @field_validator("catalog", "schedule", "discovery", mode="before")
    @classmethod
    def _one_name_is_a_list(cls, value):
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _known_drivers_with_the_capability(self):
        self.library = check_role("library", [self.library])[0]
        for role in ("catalog", "schedule", "discovery"):
            setattr(self, role, check_role(role, getattr(self, role)))
        return self


class OptimizerConfig(BaseModel):
    # 3 since 0.16 (the example and every packaged install already ran with 3); the one-time import stores 1 for a
    # config.yaml that relied on the old default (penguwatch.settings.legacy.OLD_DEFAULTS)
    max_active_services: int = Field(default=3, ge=1, le=10)
    household_priority_boost: float = Field(default=2.0, ge=0, le=10)
    ready_to_binge_boost: float = Field(default=1.5, ge=0, le=10)
    # The viewing window for "used" and the cost per hour (was optimizer.engine.VALUE_WINDOW_DAYS)
    value_window_days: int = Field(default=90, ge=30, le=365)
    # Below this many hours in the window a service counts as "not used" (was optimizer.engine.UNUSED_HOURS)
    unused_hours: float = Field(default=0.5, ge=0, le=20)


class HouseholdConfig(BaseModel):
    """Who watches. Two names add "Both" and the "who wants to watch" field; one name hides that field."""

    members: List[str] = Field(default_factory=lambda: ["Me", "Partner"], min_length=1, max_length=2,
                               json_schema_extra={"ui_type": "names"})

    @field_validator("members")
    @classmethod
    def _clean_names(cls, names: List[str]) -> List[str]:
        cleaned = [str(n).strip() for n in names]
        if any(not n or len(n) > 40 for n in cleaned):
            raise ValueError("household.members: each name needs 1 to 40 characters")
        return cleaned

    @property
    def shared(self) -> bool:
        return len(self.members) == 2

    def labels(self) -> Dict[str, str]:
        """Tag value -> name shown (HouseholdTag values)."""
        if not self.shared:
            return {"person1": self.members[0]}
        return {"both": "Both", "person1": self.members[0], "person2": self.members[1]}


class DisplayConfig(BaseModel):
    """How the page looks. `theme` is the palette a browser shows until its person picks one in the header menu. An id
    that no theme has is not an error here (themes are folders, found at run time): the page falls back to `default`
    and the log says so once (penguwatch.web.themes.resolve_default)."""

    theme: str = Field(default="default", json_schema_extra={"ui_type": "theme"})
    # The Grafana base URL the page header links to; "" = no links (PENGUWATCH_GRAFANA_URL wins while set)
    grafana_url: str = ""

    @field_validator("theme")
    @classmethod
    def _theme_id(cls, value: str) -> str:
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise ValueError("display.theme: a theme id, lower-case letters, digits and dashes (for example nord)")
        return value

    @field_validator("grafana_url")
    @classmethod
    def _grafana_url(cls, value: str) -> str:
        return plain_base_url(value, "display.grafana_url")


class NotifyConfig(BaseModel):
    """The daily digest (config.yaml `notify:`, new in 0.16; before, only the PENGUWATCH_NOTIFY_* variables). The
    digest's address is not here: it is the secret slot `notify.url` (secrets.json, or PENGUWATCH_NOTIFY_URL)."""

    # ntfy: an ntfy topic; webhook: a JSON POST (PENGUWATCH_NOTIFY_KIND maps any value other than ntfy to webhook)
    kind: Literal["ntfy", "webhook"] = "ntfy"
    # Local hour (server.timezone) of the daily check
    hour: int = Field(default=9, ge=0, le=23, json_schema_extra={"ui_type": "hour"})
    # A renewal this many days away (or fewer) is in the digest (was notify.RENEWAL_WARNING_DAYS)
    renewal_warning_days: int = Field(default=3, ge=1, le=14)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class HistoryConfig(BaseModel):
    """The History tab (config.yaml `history:`, new in 0.16)."""

    # Episodes sharing one exact watched stamp from this many count as a bulk mark (was history.BULK_MIN)
    bulk_min: int = Field(default=3, ge=2, le=20)


class RatingsConfig(BaseModel):
    """Custom rating sources (config.yaml `ratings:`); more can be added through the API."""

    sources: List[RatingSourceSpec] = Field(default_factory=list, max_length=20)

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, sources: List[RatingSourceSpec]) -> List[RatingSourceSpec]:
        ids = [s.id for s in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("ratings.sources: each id may appear once")
        return sources


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WEBHOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
WEBHOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def webhook_id(name: Optional[str], taken: Iterable[str] = ()) -> str:
    """The id a webhook gets from its name: lower-cased, anything but a-z, 0-9, '_' and '-' becomes '-', at most 40,
    "webhook" for a name with none of those, and "-2", "-3", ... when the id is already in `taken`. The id names the
    webhook's secret slots (webhook.<id>.url, webhook.<id>.secret) and is fixed once saved."""
    base = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-_")[:40].strip("-_") or "webhook"
    taken = set(taken)
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        suffix = f"-{number}"
        candidate = base[:40 - len(suffix)].rstrip("-_") + suffix
    return candidate


class WebhookItem(BaseModel):
    """One event webhook as the settings store keeps it: no URL (that is the secret slot `webhook.<id>.url`) and no
    secret (the slot `webhook.<id>.secret`, or `secret_env` / `secret_file`). `id` is None until AppConfig (or the
    store) derives it from the name, and fixed once saved."""

    model_config = ConfigDict(hide_input_in_errors=True)  # a ValidationError must not echo a secret or an address

    id: Optional[str] = None
    name: Optional[str] = None  # shown on the System tab; defaults to the host of the url (required on the page)
    events: List[str] = Field(default_factory=lambda: list(EVENT_TYPES))
    secret_env: Optional[str] = None
    secret_file: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if value and not WEBHOOK_ID_RE.fullmatch(value):
            raise ValueError("webhooks: id is 1 to 40 lower-case letters, digits, '_' or '-', starting with a letter "
                             "or digit")
        return value or None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _WEBHOOK_NAME_RE.fullmatch(value):
            raise ValueError("webhooks: name is 1 to 40 letters, digits, spaces, '.', '_' or '-'")
        return value

    @field_validator("events")
    @classmethod
    def _known_events(cls, value: List[str]) -> List[str]:
        unknown = [e for e in value if e not in EVENT_TYPES]
        if unknown:
            raise ValueError(f"webhooks: unknown event {unknown[0]!r}; known: {', '.join(EVENT_TYPES)}")
        if not value:
            raise ValueError("webhooks: events must not be empty (leave it out for all events)")
        return list(dict.fromkeys(value))

    @field_validator("secret_env")
    @classmethod
    def _env_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _ENV_NAME_RE.fullmatch(value):
            raise ValueError("webhooks: secret_env is the NAME of an environment variable, not the secret itself")
        return value

    @field_validator("secret_file")
    @classmethod
    def _file_path(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and ("\0" in value or not value.strip()):
            raise ValueError("webhooks: secret_file is a file path")
        return value

    @model_validator(mode="after")
    def _one_secret_source(self):
        if self.secret_env and self.secret_file:
            raise ValueError("webhooks: use secret_env or secret_file, not both")
        return self


class WebhookConfig(WebhookItem):
    """One event webhook as config.yaml writes it (`webhooks:`): the item plus its `url`. The secret is never written
    here: `secret_env` names an environment variable and `secret_file` a file (Docker/Podman secrets) that holds
    it."""

    url: str

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("webhooks: url must be an http:// or https:// address")
        try:
            parts.port  # noqa: B018  (raises for a port outside 0-65535 or not a number)
        except ValueError:
            raise ValueError("webhooks: the port in url is not valid") from None
        return value


class McpConfig(BaseModel):
    """`mcp:` in config.yaml (docs/mcp.md). `enabled` serves the read-only MCP endpoint (POST /mcp) that lets an AI
    assistant ask about the library; off by default, and /mcp is 404 while it is off. Guarded like the API."""

    enabled: bool = False


class AppConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    drivers: DriversConfig = Field(default_factory=DriversConfig)
    # Each driver's options: the config.yaml section named after the driver, checked by its options_model
    driver_sections: Dict[str, Any] = Field(default_factory=dict, exclude=True)
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)
    household: HouseholdConfig = Field(default_factory=HouseholdConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    services: List[ServiceConfig] = Field(default_factory=list)
    ratings: RatingsConfig = Field(default_factory=RatingsConfig)
    webhooks: List[WebhookConfig] = Field(default_factory=list, max_length=10)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="before")
    @classmethod
    def _collect_driver_sections(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            sections = dict(data.pop("driver_sections", None) or {})
            for name in DRIVERS:
                if name in data:
                    sections[name] = data.pop(name)
            data["driver_sections"] = sections
        return data

    @model_validator(mode="after")
    def _parse_driver_options(self):
        for name, driver in DRIVERS.items():
            model, raw = driver.options_model, self.driver_sections.get(name)
            if model is not None and not isinstance(raw, model):
                self.driver_sections[name] = model(**(raw or {}))
        return self

    def driver_options(self, name: str):
        """The options of a driver (its section, parsed by its options_model)."""
        return self.driver_sections.get(name)

    @field_validator("webhooks")
    @classmethod
    def _unique_webhook_names(cls, hooks: List[WebhookConfig]) -> List[WebhookConfig]:
        seen = set()
        for hook in hooks:
            hook.name = hook.name or (urlsplit(hook.url).hostname or "webhook")[:40]
            if hook.name in seen:
                raise ValueError(f"webhooks: the name {hook.name!r} is used twice; give each webhook its own name")
            seen.add(hook.name)
        ids = [hook.id for hook in hooks if hook.id]
        if len(ids) != len(set(ids)):
            raise ValueError("webhooks: each id may appear once")
        taken = set(ids)
        for hook in hooks:
            if not hook.id:
                hook.id = webhook_id(hook.name, taken)
                taken.add(hook.id)
        return hooks


class EnvOverrides(BaseSettings):
    """The environment variables PenguWatch reads, parsed and validated once (the OTEL_* variables belong to the
    OpenTelemetry SDK, TZ to the clock, and the PENGUWATCH_ACCESS_TOKEN family to the security guards). Unset and
    empty values mean "not set"."""

    # hide_input_in_errors: a refused variable (PENGUWATCH_NOTIFY_URL is a credential) is named, never quoted
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore", hide_input_in_errors=True)

    host: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_HOST")
    # Kept as str: an unparsable port is ignored in load_config, not an error.
    port: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_PORT")
    log_level: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_LOG_LEVEL")
    db_path: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_DB_PATH")
    # The config file to read (set by the `penguwatch` command and the container); otherwise the search in load_config
    config_file: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_CONFIG")
    timezone: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_TZ")
    # The drivers' own variables (each driver's env_options), {variable: value} for those set and not empty
    driver_values: Dict[str, str] = Field(default_factory=dict, exclude=True)
    # Read before config.yaml is loaded, to set up logging
    log_format: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_LOG_FORMAT")
    # The daily digest: an ntfy topic or webhook URL ("" = off; the secret slot notify.url), and, over the settings
    # notify.kind and notify.hour, its kind (ntfy; any other value means webhook) and the local hour of the check.
    # None: not set (an empty variable is not set either), so the setting applies
    # The variables that pin a setting are kept as written here and checked by the setting's own field when they are
    # applied (apply_env, env_pins): one the field refuses is ignored with a warning, it never stops the start.
    notify_url: str = Field(default="", validation_alias="PENGUWATCH_NOTIFY_URL")
    notify_kind: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_NOTIFY_KIND")
    notify_hour: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_NOTIFY_HOUR")
    # Links to Grafana in the page header (over the setting display.grafana_url)
    grafana_url: str = Field(default="", validation_alias="PENGUWATCH_GRAFANA_URL")

    # Turns the read-only MCP endpoint (/mcp) on or off, over the setting mcp.enabled
    mcp_enabled: Optional[str] = Field(default=None, validation_alias="PENGUWATCH_MCP_ENABLED")

    @field_validator("mcp_enabled", "notify_hour", "notify_kind", "log_level", "timezone", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("grafana_url")
    @classmethod
    def _grafana_url_stripped(cls, value: str) -> str:
        """The URL as plain_base_url writes it when it is one; otherwise as written (apply_env ignores it)."""
        try:
            return plain_base_url(value)
        except ValueError:
            return value.strip()

    @property
    def notify_kind_choice(self) -> Optional[str]:
        """PENGUWATCH_NOTIFY_KIND as the setting notify.kind reads it: "ntfy", any other value "webhook", None unset."""
        if self.notify_kind is None:
            return None
        return "ntfy" if self.notify_kind.strip().lower() == "ntfy" else "webhook"

    @model_validator(mode="after")
    def _read_driver_values(self):
        for driver in DRIVERS.values():
            for variable, _ in getattr(driver, "env_options", ()):
                value = os.environ.get(variable)
                if value and variable not in self.driver_values:
                    self.driver_values[variable] = value
        return self


# (attribute on EnvOverrides, section on AppConfig, field on that section): bootstrap keys, applied as written
_STRING_OVERRIDES = [
    ("host", "server", "host"),
    ("db_path", "database", "db_path"),
]

# (attribute on EnvOverrides, section on AppConfig, field, variable): settings a variable pins (penguwatch.settings.env
# SETTING_ENV), each checked by its section's field; a value the field refuses stops the start, naming the variable
_SETTING_OVERRIDES = [
    ("log_level", "server", "log_level", "PENGUWATCH_LOG_LEVEL"),
    ("timezone", "server", "timezone", "PENGUWATCH_TZ"),
    ("grafana_url", "display", "grafana_url", "PENGUWATCH_GRAFANA_URL"),
    ("notify_kind_choice", "notify", "kind", "PENGUWATCH_NOTIFY_KIND"),
    ("notify_hour", "notify", "hour", "PENGUWATCH_NOTIFY_HOUR"),
    ("mcp_enabled", "mcp", "enabled", "PENGUWATCH_MCP_ENABLED"),
]


PACKAGED_EXAMPLE = "config.example.yaml"


def packaged_example_text() -> str:
    """The example config that ships inside the package (a copy of the repository's config.example.yaml)."""
    return resources.files("penguwatch").joinpath(PACKAGED_EXAMPLE).read_text(encoding="utf-8")


def find_config_file(config_path: Optional[str] = None, env: Optional[EnvOverrides] = None) -> Optional[str]:
    """The config file load_config reads: the argument, PENGUWATCH_CONFIG (set by the `penguwatch` command and the
    container), your config.yaml next to where you run it or in the checkout, /app/config.yaml, config.example.yaml.
    None: none of them, so the example inside the package is read. A named file need not exist (then: the
    defaults)."""
    env = env or EnvOverrides()
    if config_path:
        return config_path
    if env.config_file:
        return env.config_file
    # Search relative to CWD or package root
    # Your config.yaml first; the shipped config.example.yaml (defaults) when there is none
    root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        Path("config.yaml"),
        root / "config.yaml",
        Path("/app/config.yaml"),
        Path("config.example.yaml"),
        root / "config.example.yaml",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def read_config_file(config_path: Optional[str] = None,
                     env: Optional[EnvOverrides] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """(the file read, its raw mapping): what load_config validates. The path is None when the packaged example was
    read; the mapping is {} for a named file that does not exist or is empty. Raises yaml.YAMLError for a file that is
    not YAML, ValueError for one whose top level is not a mapping. The one-time settings import reads this, never
    the strict AppConfig, so one bad value costs that value only."""
    path = find_config_file(config_path, env)
    if path is None:  # an installed package has no checkout: its own copy of the example
        data = yaml.safe_load(packaged_example_text()) or {}
    elif Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path or PACKAGED_EXAMPLE}: the top level must be a mapping of sections")
    return path, data


# The keys config.yaml keeps after the one-time settings import: needed before the database (and the settings in it)
# opens. load_config reads them strictly (a bad one stops the start); every other key leniently.
BOOTSTRAP_KEYS = ("server.host", "server.port", "database.db_path")


def _is_bootstrap(path: str) -> bool:
    return any(path == key or key.startswith(path + ".") or path.startswith(key + ".") for key in BOOTSTRAP_KEYS)


def lenient_config(data: Dict[str, Any], where: Optional[str] = None) -> Tuple[AppConfig, List[Tuple[str, str]]]:
    """(AppConfig from config.yaml's mapping, [(path, reason)] left out). Strict for the bootstrap keys (ValueError
    naming the key, never its value), lenient for the rest: a value the models refuse costs that value only (its
    default is used), as a stale config.yaml must not stop the start once the settings live in the database."""
    from penguwatch.settings import lenient  # plain pydantic: imports nothing of the app
    config, dropped = lenient.validate_leniently(AppConfig, data if isinstance(data, dict) else {})
    for path, why in dropped:
        if _is_bootstrap(path):
            raise ValueError(f"{where or PACKAGED_EXAMPLE}: {path}: {why}")
    return config, dropped


def load_config(config_path: Optional[str] = None, env: Optional[EnvOverrides] = None) -> AppConfig:
    """The config file (read_config_file says which), read strictly for the bootstrap keys and leniently for the rest
    (a refused value is logged by its key, never its value, and its default used), with the environment applied
    (`env`, read from the environment when not given; apply_env). After the one-time settings import only the
    bootstrap keys of this matter: the settings manager builds the running config from the store."""
    env = env or EnvOverrides()
    path, data = read_config_file(config_path, env)
    config, dropped = lenient_config(data, path)
    for key, why in dropped:
        logger.warning(f"{path or PACKAGED_EXAMPLE}: {key} left out ({why}); its default is used")
    return apply_env(config, env)


def _checked(model: type, field: str, value: Any, variable: str) -> Any:
    """`value` as the section model's field reads it; ValueError naming the variable (never the value) when it
    refuses."""
    try:
        return getattr(model.model_validate({field: value}), field)
    except ValidationError as e:
        message = e.errors()[0].get("msg", "not valid").removeprefix("Value error, ")
        raise ValueError(f"{variable}: {message}") from None


def env_pins(env: EnvOverrides) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """({registry key: the value its field reads}, {registry key: why the variable is ignored}) for the variables
    that pin a setting (penguwatch.settings.env.SETTING_ENV; unset or empty = the setting applies). A variable the
    field refuses is ignored: the setting stays editable and the reason (naming the variable, never its value) is
    shown next to it on the Settings tab and by `penguwatch doctor`."""
    valid: Dict[str, Any] = {}
    ignored: Dict[str, str] = {}
    for attr, section, field, variable in _SETTING_OVERRIDES:
        value = getattr(env, attr)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        key = f"{section}.{field}"
        model = AppConfig.model_fields[section].annotation
        try:
            valid[key] = _checked(model, field, value, variable)
        except ValueError as e:
            message = str(e).removeprefix(f"{variable}: ")
            ignored[key] = f"{variable} is set but not valid ({message}): ignored"
    return valid, ignored


_warned: set = set()  # the ignored variables already logged by this process


def apply_env(config: AppConfig, env: EnvOverrides) -> AppConfig:
    """Applies the environment over `config` in place and returns it: the bootstrap variables (host, port, database
    path), the drivers' variables (each driver's env_options), and every variable that pins a setting
    (penguwatch.settings.env.SETTING_ENV; unset or empty = the setting applies; one its field refuses is ignored and
    logged once as a warning naming it). The settings manager builds the effective config with it."""
    for attr, section, field in _STRING_OVERRIDES:
        if value := getattr(env, attr):
            setattr(getattr(config, section), field, value)
    valid, ignored = env_pins(env)
    for key, value in valid.items():
        section, field = key.split(".")
        setattr(getattr(config, section), field, value)
    for key, why in ignored.items():
        if why not in _warned:
            _warned.add(why)
            logger.warning(f"{why}; the setting {key} applies")
    for name, driver in DRIVERS.items():
        options = config.driver_options(name)
        for variable, option in getattr(driver, "env_options", ()):
            if options is not None and (value := env.driver_values.get(variable)):
                setattr(options, option, value)
    if env.port:
        try:
            config.server.port = int(env.port)
        except ValueError:
            pass

    return config
