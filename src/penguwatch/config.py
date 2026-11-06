"""Configuration management for PenguWatch."""

import re
from urllib.parse import urlsplit
from importlib import resources
from pathlib import Path
import os
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from penguwatch.drivers import DEFAULT_CHAIN, DEFAULT_LIBRARY, DRIVERS, check_role
from penguwatch.events import EVENT_TYPES
from penguwatch.models import ServiceConfig
from penguwatch.ratings import RatingSourceSpec


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    # IANA zone for household-day decisions; empty = TZ env var, else UTC
    timezone: str = ""


class DatabaseConfig(BaseModel):
    db_path: str = "data/penguwatch.db"


class DriversConfig(BaseModel):
    """Which driver fills each role (config.yaml `drivers:`; docs/drivers.md). One library driver; the catalog,
    schedule and discovery roles take a list, tried in order (the first that has the title wins); an empty schedule or
    discovery list turns that feature off, while the catalog needs at least one driver. Checked at start-up:
    an unknown driver, or one without the capability its role needs, stops the start with a message naming it."""

    model_config = ConfigDict(extra="forbid")

    library: str = DEFAULT_LIBRARY
    catalog: List[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN))
    schedule: List[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN))
    discovery: List[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN))

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
    max_active_services: int = 1
    household_priority_boost: float = 2.0
    ready_to_binge_boost: float = 1.5


class HouseholdConfig(BaseModel):
    """Who watches. Two names add "Both" and the "who wants to watch" field; one name hides that field."""

    members: List[str] = Field(default_factory=lambda: ["Me", "Partner"], min_length=1, max_length=2)

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


class WebhookConfig(BaseModel):
    """One event webhook (config.yaml `webhooks:`). The secret is never written here: `secret_env` names an
    environment variable and `secret_file` a file (Docker/Podman secrets) that holds it."""

    model_config = ConfigDict(hide_input_in_errors=True)  # a ValidationError must not echo a secret or an address

    name: Optional[str] = None  # shown on the System tab; defaults to the host of the url
    url: str
    events: List[str] = Field(default_factory=lambda: list(EVENT_TYPES))
    secret_env: Optional[str] = None
    secret_file: Optional[str] = None

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

    @model_validator(mode="after")
    def _one_secret_source(self):
        if self.secret_env and self.secret_file:
            raise ValueError("webhooks: use secret_env or secret_file, not both")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    drivers: DriversConfig = Field(default_factory=DriversConfig)
    # Each driver's options: the config.yaml section named after the driver, checked by its options_model
    driver_sections: Dict[str, Any] = Field(default_factory=dict, exclude=True)
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)
    household: HouseholdConfig = Field(default_factory=HouseholdConfig)
    services: List[ServiceConfig] = Field(default_factory=list)
    ratings: RatingsConfig = Field(default_factory=RatingsConfig)
    webhooks: List[WebhookConfig] = Field(default_factory=list, max_length=10)

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
        return hooks


_GRAFANA_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")


def plain_base_url(value: str, name: str = "PENGUWATCH_GRAFANA_URL") -> str:
    """A Grafana base URL without the trailing slash; "" for none. Raises ValueError for anything that is not a plain
    http(s) base URL (no query, fragment or credentials)."""
    url = (value or "").strip().rstrip("/")
    if url and not _GRAFANA_URL_RE.fullmatch(url):
        raise ValueError(f"{name} must be a plain http(s) base URL such as https://grafana.example:3000")
    return url


class EnvOverrides(BaseSettings):
    """The environment variables PenguWatch reads, parsed and validated once (the OTEL_* variables belong to the
    OpenTelemetry SDK, TZ to the clock, and the PENGUWATCH_ACCESS_TOKEN family to the security guards). Unset and
    empty values mean "not set"."""

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

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
    # The daily digest: an ntfy topic or webhook URL ("" = off), its kind, and the local hour of the check
    notify_url: str = Field(default="", validation_alias="PENGUWATCH_NOTIFY_URL")
    notify_kind: str = Field(default="ntfy", validation_alias="PENGUWATCH_NOTIFY_KIND")
    notify_hour: int = Field(default=9, validation_alias="PENGUWATCH_NOTIFY_HOUR")
    # Links to Grafana in the page header; an invalid URL stops the start
    grafana_url: str = Field(default="", validation_alias="PENGUWATCH_GRAFANA_URL")

    @field_validator("notify_hour", mode="before")
    @classmethod
    def _blank_hour_is_the_default(cls, value):
        return 9 if isinstance(value, str) and not value.strip() else value

    @field_validator("grafana_url")
    @classmethod
    def _grafana_url_is_a_plain_base_url(cls, value: str) -> str:
        return plain_base_url(value)

    @model_validator(mode="after")
    def _read_driver_values(self):
        for driver in DRIVERS.values():
            for variable, _ in getattr(driver, "env_options", ()):
                value = os.environ.get(variable)
                if value and variable not in self.driver_values:
                    self.driver_values[variable] = value
        return self


# (attribute on EnvOverrides, section on AppConfig, field on that section)
_STRING_OVERRIDES = [
    ("host", "server", "host"),
    ("log_level", "server", "log_level"),
    ("db_path", "database", "db_path"),
    ("timezone", "server", "timezone"),
]


PACKAGED_EXAMPLE = "config.example.yaml"


def packaged_example_text() -> str:
    """The example config that ships inside the package (a copy of the repository's config.example.yaml)."""
    return resources.files("penguwatch").joinpath(PACKAGED_EXAMPLE).read_text(encoding="utf-8")


def load_config(config_path: Optional[str] = None, env: Optional[EnvOverrides] = None) -> AppConfig:
    """Loads configuration from YAML file, with environment variable overrides (`env`, read from the environment when
    not given).

    The file is, in order: the argument, PENGUWATCH_CONFIG (set by the `penguwatch` command and the container), your
    config.yaml next to where you run it or in the checkout, /app/config.yaml, config.example.yaml, and last the
    example inside the package. A named file that does not exist means the defaults."""
    env = env or EnvOverrides()
    if not config_path:
        config_path = env.config_file or None
    if not config_path:
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
                config_path = str(c)
                break

    if not config_path:  # an installed package has no checkout: its own copy of the example
        data = yaml.safe_load(packaged_example_text()) or {}
        return _with_env(AppConfig(**data), env)

    data = {}
    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    return _with_env(AppConfig(**data), env)


def _with_env(config: AppConfig, env: EnvOverrides) -> AppConfig:
    # Environment variable overrides
    for attr, section, field in _STRING_OVERRIDES:
        if value := getattr(env, attr):
            setattr(getattr(config, section), field, value)
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
