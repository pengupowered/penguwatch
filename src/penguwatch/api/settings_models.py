"""The request and response bodies of the Settings API (api/settings.py): the one place their shapes are written
down. The Settings tab and the settings store code against these; docs/api.md ("Settings") has examples.

Conventions:
- A key is a registry key ("optimizer.max_active_services", "tvmaze.timeout_seconds"); the one list is `webhooks`; a
  slot is a secret slot ("notify.url", "webhook.<id>.url", "webhook.<id>.secret").
- A secret's value is only ever sent *to* the server (PUT `secrets`), never back: answers carry {set, source, host}.
- Every non-2xx answer of a settings route is SettingsError (409: ConflictError). `message` never repeats the input.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["default", "stored", "env"]
SecretSource = Literal["stored", "env", "file"]  # secrets.json; a variable; the file a webhook's secret_file names
VersionSource = Literal["import", "save", "restore", "yaml_import"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- errors (every non-2xx answer of a settings route) ----

class FieldError(_Model):
    """`where`: "values.<key>", "lists.webhooks[<index>].<field>" or "secrets.<slot>". `message` never repeats the
    submitted value."""
    where: str
    message: str


class SettingsError(_Model):
    detail: str
    errors: List[FieldError] = Field(default_factory=list)


class ConflictError(_Model):
    """409 from PUT: parts this request touched changed since `base_revision`."""
    detail: str
    revision: int
    conflicts: List[str]  # keys, list names and slots


# ---- GET /api/settings/schema ----

class SectionSpec(_Model):
    id: str
    title: str
    order: int


FieldType = Literal["number", "integer", "boolean", "text", "choice", "timezone", "theme", "names", "hour", "driver",
                    "drivers", "list", "object", "map"]


class Constraints(_Model):
    type: FieldType
    nullable: bool
    min: Optional[float] = None
    max: Optional[float] = None
    exclusive_min: bool = False
    exclusive_max: bool = False
    min_length: Optional[int] = None  # text: characters; names / drivers / list: items
    max_length: Optional[int] = None
    step: Optional[float] = None  # 1 for integers; None: any
    choices: Optional[List[Any]] = None


class SettingSpec(Constraints):
    key: str
    section: str
    group: Optional[str]
    label: str
    effect: str
    unit: Optional[str]
    live: bool  # False: "Applies after a restart"
    env: Optional[str]  # the variable that pins it when set
    advanced: bool
    export: bool
    order: int
    choice_labels: Optional[Dict[str, str]] = None  # stored choice -> the words shown for it (theme ids -> names)
    empty: Optional[str] = None  # what an empty value means, in words
    default: Any


class ItemFieldSpec(Constraints):
    name: str
    required: bool
    default: Any


class ListSchema(_Model):
    label: str
    section: str
    path: str  # where it lives in config.yaml / an export ("webhooks")
    max_items: int
    item_fields: List[ItemFieldSpec]
    default_items: List[Dict[str, Any]]


class SecretSpec(_Model):
    slot: str  # "notify.url"; a pattern: "webhook.{id}.url", "webhook.{id}.secret"
    label: str
    kind: Literal["url", "token"]
    section: str
    env: Optional[str]
    pattern: bool


class DriverSpec(_Model):
    name: str  # "tvmaze"
    label: str  # "TVmaze"
    offers: List[str]  # capabilities: catalog, library_sync, schedule, discovery, ratings
    remote: bool  # False: the local library (no account)


class RoleSpec(_Model):
    role: str  # library | catalog | schedule | discovery (the setting drivers.<role>)
    capability: str  # what a driver needs to fill it
    multiple: bool  # an ordered list (all but library)
    optional: bool  # may be empty: the feature is off (schedule, discovery)


class ThemeSpec(_Model):
    id: str
    name: str
    description: str


class SettingsSchema(_Model):
    version: str  # the app version; also the ETag
    sections: List[SectionSpec]
    settings: List[SettingSpec]
    lists: Dict[str, ListSchema]
    secrets: List[SecretSpec]
    drivers: List[DriverSpec]
    roles: List[RoleSpec]
    themes: List[ThemeSpec]


# ---- GET /api/settings ----

class ValueState(_Model):
    value: Any  # the effective value (env > stored > default)
    stored: Any = None  # the stored value; None when the key is not stored
    source: Source
    pending_restart: bool = False  # live: False and the stored value differs from the running one
    live_failed: bool = False  # the last save's applier for it raised: it applies at the next start
    # The setting's variable is set but its field refuses it, so it is ignored and the setting stays editable: why, in
    # words that name the variable and never its value ("PENGUWATCH_TZ is set but not valid (...): ignored")
    env_ignored: Optional[str] = None


class ItemState(_Model):
    """How a stored webhook runs: ok; address_needed (its URL slot is empty: it stays off); disabled (its signing
    secret cannot be read: `reason` says why, naming a variable or file, never a value)."""
    state: Literal["ok", "address_needed", "disabled"]
    reason: Optional[str] = None


class ListState(_Model):
    set: bool  # False: the list's default (`items` are then the defaults: none)
    items: List[Dict[str, Any]]  # WebhookItem dumps: id, name, events, secret_env, secret_file (never a url)
    states: Dict[str, ItemState] = Field(default_factory=dict)  # item id -> state


class SecretState(_Model):
    set: bool  # a value is there (from `source`)
    source: Optional[SecretSource] = None  # where it comes from; None when not set and nothing names a source
    host: Optional[str] = None  # URL slots only: the host, as the page shows it ("Set · ntfy.example.com")


class ConfigYamlState(_Model):
    ignored_since: Optional[float] = None  # when the import ran (epoch seconds); None: not imported yet
    differing_keys: int = 0  # settings keys config.yaml sets to something else than the page (names in the log)
    differing_services: int = 0  # services config.yaml lists differently from the services table


class ImportNote(_Model):
    key: str  # a registry key, "webhooks[<n>]", "services", "ratings.sources[<n>]", "<driver>.client_id", ...
    reason: str  # in words; never a value


class About(_Model):
    """What stays outside the settings store. Sent to every caller who got through the access guard."""
    version: str
    listen: str  # "0.0.0.0:8080" (the effective host and port)
    data_dir: str  # the folder of the database (secrets.json and sessions.json are next to it)
    config_path: Optional[str]  # the config.yaml read; None: the example inside the package
    config_exists: bool
    db_path: str
    secrets_path: str  # <folder of the database>/secrets.json
    token_set: bool  # PENGUWATCH_ACCESS_TOKEN is set
    config_yaml: Optional[ConfigYamlState] = None
    import_report: List[ImportNote] = Field(default_factory=list)


class SettingsState(_Model):
    revision: int  # 0: no settings store yet
    values: Dict[str, ValueState]  # every registry key
    lists: Dict[str, ListState]  # "webhooks"
    secrets: Dict[str, SecretState]  # "notify.url" always; "webhook.<id>.url" / ".secret" per webhook item
    about: About
    token_set: bool  # the page's banner: no token = anyone who can reach the port may change settings


class SettingsValues(_Model):
    """GET /api/settings/values: the effective values only."""
    revision: int
    values: Dict[str, Any]


# ---- POST /api/settings/validate, PUT /api/settings ----

class SettingsChange(_Model):
    """A change against the current state: values[key] = None resets the key, lists[name] = None resets the list."""
    values: Dict[str, Any] = Field(default_factory=dict)
    lists: Dict[str, Optional[List[Dict[str, Any]]]] = Field(default_factory=dict)


class SettingsValidate(SettingsChange):
    secrets: Dict[str, str] = Field(default_factory=dict)  # slot -> new value; "" clears the slot


class ValidateResponse(_Model):
    ok: bool
    errors: List[FieldError] = Field(default_factory=list)
    warnings: List[FieldError] = Field(default_factory=list)  # not refusals ("MCP on with no access token")
    summary: str = ""  # the version line the save would write


class SettingsPut(SettingsValidate):
    base_revision: int


class PutResponse(_Model):
    revision: int
    summary: str
    applied: List[str]  # keys, list names and slots that changed
    restart_needed: List[str] = Field(default_factory=list)  # keys with live: False that changed
    live_failed: List[str] = Field(default_factory=list)  # committed, but an applier raised: at the next start
    reload: bool = False  # the page reloads (household.members changed: the names are in the page's <meta>)


# ---- versions, export, import ----

class VersionInfo(_Model):
    revision: int
    applied_at: float
    source: VersionSource
    summary: str


class VersionDraft(_Model):
    """GET /api/settings/versions/{n}: the document of that version, as a draft (saving it restores it)."""
    revision: int
    values: Dict[str, Any]
    lists: Dict[str, Optional[List[Dict[str, Any]]]]


class ImportDraft(_Model):
    """POST /api/settings/import: a draft from YAML; nothing is applied."""
    values: Dict[str, Any]
    lists: Dict[str, Optional[List[Dict[str, Any]]]]
    notes: List[str] = Field(default_factory=list)  # what was left out and where it is edited instead
    kept: List[str] = Field(default_factory=list)  # keys whose value in the file was refused: the current one stays
