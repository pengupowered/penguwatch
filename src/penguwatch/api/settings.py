"""The Settings API: the settings store (penguwatch.settings.manager) read, validated, saved, versioned, exported and
imported. Bodies: api/settings_models.py; examples: docs/api.md ("Settings").

GET  /api/settings/schema         the registry with types, ranges, defaults, the list's fields, the secret slots, the
                                  drivers and roles, the themes
GET  /api/settings                every value with its source, the webhooks (never a URL), the secret slots (set or
                                  not, never a value) and "about this install"
GET  /api/settings/values         {revision, values}: the effective values only
POST /api/settings/validate       a change {values, lists, secrets} -> {ok, errors, warnings, summary}; nothing applied
PUT  /api/settings                {base_revision, values, lists, secrets} -> the new revision (?source=restore for a
                                  version loaded with GET /versions/{n}, ?source=yaml_import for an Import draft)
GET  /api/settings/versions       the kept versions, newest first
GET  /api/settings/versions/{n}   that version as a draft (saving it restores it)
GET  /api/settings/export         the stored values and the webhooks as YAML, no secret
POST /api/settings/import         YAML in the body -> a draft {values, lists, notes, kept}; nothing is applied

Guarded like the rest of /api (never a public path). A secret's value only ever comes in (PUT `secrets`); no answer,
log line or error message carries one. Errors: {"detail", "errors": [{"where", "message"}]}; a 409 adds the current
revision and the conflicting parts. Not in api/stable.STABLE: a page-only API that may change.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from penguwatch import __version__, paths
from penguwatch.api.deps import AppContainer
from penguwatch.api.settings_models import (
    About, ConfigYamlState, ConflictError, ImportDraft, ImportNote, ItemState, ListState, PutResponse, SecretState,
    SettingsError, SettingsPut, SettingsSchema, SettingsState, SettingsValidate, SettingsValues, ValidateResponse,
    ValueState, VersionDraft, VersionInfo,
)
from penguwatch.db.settings import SettingsConflict
from penguwatch.security import rate_limit
from penguwatch.settings.manager import SettingsInvalid, SettingsManager, SettingsWriteFailed
from penguwatch.settings.schema import build_schema
from penguwatch.settings.yamlsafe import IMPORT_MAX_BYTES

router = APIRouter(tags=["Settings"])

YAML_TYPES = ("text/yaml", "application/x-yaml", "application/yaml", "text/plain")


def _container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None or container.settings is None:
        raise HTTPException(status_code=503, detail="Settings are not loaded yet")
    return container


def _manager(request: Request) -> SettingsManager:
    return _container(request).settings


def _error(status: int, detail: str, errors=()) -> JSONResponse:
    return JSONResponse(status_code=status, content=SettingsError(detail=detail, errors=list(errors)).model_dump())


def _token_set(container: AppContainer) -> bool:
    return bool(getattr(container.security, "access_token", "") or "")


def _about(container: AppContainer, manager: SettingsManager) -> About:
    config = container.config
    db_path = str(getattr(container.repository, "db_path", "") or config.database.db_path)
    config_path = manager.config_path
    return About(
        version=__version__,
        listen=f"{config.server.host}:{config.server.port}",
        data_dir=str(Path(db_path).parent),
        config_path=config_path,
        config_exists=config_path is None or Path(config_path).is_file(),
        db_path=db_path,
        secrets_path=str(paths.secrets_path(db_path)),
        token_set=_token_set(container),
        config_yaml=ConfigYamlState(**manager.config_yaml_state()),
        import_report=[ImportNote(**r) for r in manager.import_report()],
    )


@lru_cache(maxsize=1)
def _schema() -> Dict[str, Any]:
    return build_schema()


@lru_cache(maxsize=1)
def _etag() -> str:
    digest = hashlib.sha256(json.dumps(_schema(), sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f'"settings-schema-{__version__}-{digest}"'


@router.get("/settings/schema", response_model=SettingsSchema, summary="The Settings tab's schema")
async def get_settings_schema(request: Request, response: Response):
    """The registry as the page needs it: sections, settings with type, range, choices and default, the webhooks'
    item fields, the secret slots, the drivers and roles, the themes. Static per build: ETag, revalidated on use."""
    headers = {"ETag": _etag(), "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == _etag():
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return SettingsSchema.model_validate(_schema())


@router.get("/settings", response_model=SettingsState, summary="Every setting with its source")
async def get_settings(request: Request):
    """Every setting's effective value, stored value and source, the webhooks (never a URL), the secret slots (set or
    not, never a value) and what stays outside the settings store."""
    container = _container(request)
    manager: SettingsManager = container.settings
    running = getattr(container.webhooks, "hooks", None) or []
    lists = manager.list_states(running)
    return SettingsState(
        revision=manager.revision,
        values={k: ValueState(**v) for k, v in manager.value_states().items()},
        lists={name: ListState(set=state["set"], items=state["items"],
                               states={k: ItemState(**s) for k, s in state["states"].items()})
               for name, state in lists.items()},
        secrets={k: SecretState(**v) for k, v in manager.secret_states().items()},
        about=_about(container, manager), token_set=_token_set(container))


@router.get("/settings/values", response_model=SettingsValues, summary="The effective setting values")
async def get_settings_values(request: Request):
    """{revision, values: {key: effective value}}."""
    manager = _manager(request)
    return SettingsValues(revision=manager.revision, values=manager.values())


@router.post("/settings/validate", response_model=ValidateResponse, summary="Check a settings change",
             dependencies=[Depends(rate_limit("settings-validate", 60, 60))])
async def validate_settings(body: SettingsValidate, request: Request):
    """Checks a change against the current state; nothing is applied. `warnings` are not refusals."""
    checked = await _manager(request).validate(body.values, body.lists, body.secrets)
    return ValidateResponse(ok=checked.ok, errors=checked.errors, warnings=checked.warnings, summary=checked.summary)


@router.put("/settings", response_model=PutResponse, summary="Save a settings change",
            dependencies=[Depends(rate_limit("settings", 20, 60))],
            responses={400: {"model": SettingsError}, 409: {"model": ConflictError}, 422: {"model": SettingsError},
                       500: {"model": SettingsError}})
async def put_settings(body: SettingsPut, request: Request,
                       source: str = Query("save", pattern="^(save|restore|yaml_import)$")):
    """Saves a change as the next revision and applies it. values[key] = null resets a key, lists[name] = null the
    list, secrets[slot] = "" clears a slot."""
    manager = _manager(request)
    try:
        applied = await manager.apply(body.base_revision, body.values, body.lists, body.secrets, source=source)
    except SettingsInvalid as e:
        return _error(e.status, e.detail, e.errors)
    except SettingsConflict as e:
        return JSONResponse(status_code=409, content=ConflictError(
            detail="Changed elsewhere since you opened the page", revision=e.current,
            conflicts=e.conflicts).model_dump())
    except SettingsWriteFailed as e:
        return _error(500, e.detail)
    return PutResponse(revision=applied.revision, summary=applied.summary, applied=applied.applied,
                       restart_needed=applied.restart_needed, live_failed=applied.live_failed, reload=applied.reload)


@router.get("/settings/versions", response_model=List[VersionInfo], summary="The kept settings versions")
async def get_settings_versions(request: Request):
    """The last 50 saves, newest first (revision 1 is the one-time import)."""
    return [VersionInfo(**v) for v in await _manager(request).versions()]


@router.get("/settings/versions/{revision}", response_model=VersionDraft, summary="One settings version as a draft",
            responses={404: {"model": SettingsError}})
async def get_settings_version(revision: int, request: Request):
    """That version as a draft against the current settings; saving it (PUT ?source=restore) restores it."""
    draft = await _manager(request).version(revision)
    if draft is None:
        return _error(404, "That version is no longer kept")
    return VersionDraft(**draft)


@router.get("/settings/export", summary="Export the settings as YAML")
async def export_settings(request: Request):
    """The stored values and the webhooks in config.yaml syntax, without any secret (no address, no signing secret)."""
    return Response(_manager(request).export_yaml(), media_type="text/yaml; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="penguwatch-settings.yaml"'})


@router.post("/settings/import", response_model=ImportDraft, summary="Read a settings file into a draft",
             dependencies=[Depends(rate_limit("settings-import", 20, 60))],
             responses={415: {"model": SettingsError}, 422: {"model": SettingsError}})
async def import_settings(request: Request):
    """Reads YAML (an export, or a whole config.yaml; Content-Type text/yaml, at most 64 KiB) into a draft for the
    page. Nothing is applied; secrets, the bootstrap keys, services and rating sources are left out with notes."""
    manager = _manager(request)
    kind = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if kind not in YAML_TYPES:
        return _error(415, "Send the file as text/yaml")
    raw = await request.body()
    if len(raw) > IMPORT_MAX_BYTES:
        return _error(422, f"That file is too big to be a settings file (at most {IMPORT_MAX_BYTES // 1024} KiB)")
    try:
        draft = manager.import_yaml(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _error(422, "That file is not UTF-8 text")
    except ValueError as e:
        return _error(422, str(e))
    return ImportDraft(**draft)
