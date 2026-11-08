"""FastAPI application entrypoint for PenguWatch."""

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from penguwatch import __version__, activity
from penguwatch import notify_component, telemetry_component, webhooks_component
from penguwatch.activity import ActivityMiddleware
from penguwatch.api import component as api_component
from penguwatch.mcp.route import router as mcp_router
from penguwatch.api.router import router as api_router
from penguwatch.api.stable import TAGS, mark_stable
from penguwatch.api.system import metrics_router
from penguwatch.components import run_components
from penguwatch.config import DisplayConfig, EnvOverrides, HouseholdConfig, load_config
from penguwatch.db import storage
from penguwatch.optimizer import component as optimizer_component
from penguwatch.pwa import add_pwa_routes
from penguwatch.sync import component as drivers_component, worker as sync_worker_component
from penguwatch.sync.worker import sync_worker, worker_step  # noqa: F401  (re-exported: the tests import them here)
from penguwatch.observability import ObservabilityMiddleware, configure_logging
from penguwatch.settings import component as settings_component
from penguwatch.security import SecurityConfig, SecurityMiddleware
from penguwatch import grafana, telemetry, ui as page_ui
from penguwatch.web import themes

_env = EnvOverrides()  # a pinning variable its setting refuses is ignored with a warning (config.apply_env)
configure_logging(_env)  # text or JSON (PENGUWATCH_LOG_FORMAT), request ids, credential redaction
logger = logging.getLogger("penguwatch")
try:  # the pre-start default; the settings component sets the effective value at start
    grafana.configure(_env.grafana_url)
except ValueError:
    grafana.configure("")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"
TEMPLATES_DIR = BASE_DIR / "web" / "templates"


# The parts of the backend, in the order they start (they stop in the reverse order; docs/architecture.md says why).
# Each is a module with `register(ctx)`; this list is the one place that names them.
COMPONENTS = [
    storage,            # the database; closes last of the data parts
    settings_component,  # the settings store: config.yaml imported once, the effective config into ctx.config
    drivers_component,  # the drivers (each with its stored login) and the sync engine on the library driver
    optimizer_component,
    api_component,      # the container the request handlers read
    sync_worker_component,  # the background sync; its stop flushes the outbox while the drivers are still open
    notify_component,   # optional: the daily digest
    webhooks_component,  # optional: event webhooks (after the container and the database; it listens to data changes)
    telemetry_component,  # optional: event-loop lag and the state gauges
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # OTLP export when OTEL_EXPORTER_OTLP_ENDPOINT is set. Here, not at import: `python -m penguwatch.main` imports
    # this module twice (as __main__, then uvicorn's penguwatch.main). FastAPI's request telemetry looks the global
    # providers up per request, and no request is served before startup.
    otel = telemetry.setup_telemetry()
    env = EnvOverrides()  # read once per start: what the environment holds now
    config = load_config(env=env)  # the bootstrap keys; the settings component makes the rest the store's
    os.umask(0o077)  # everything the app creates (database, WAL, backups, temp files) is private to its user
    async with run_components(app, config, COMPONENTS, env=env, security=security_config, otel=otel) as ctx:
        library = ctx.sync.library
        logger.info(
            f"PenguWatch {__version__} started "
            f"({library.label} {'connected' if library.is_connected() else 'not connected'})."
        )
        yield
    logger.info("PenguWatch shut down.")
    otel.shutdown()  # last, so the shutdown's own log lines and spans go out too


app = FastAPI(
    title="PenguWatch",
    description="TV & movie tracker, streaming subscription rotation optimizer, two-way sync with your tracker",
    version=__version__,
    lifespan=lifespan,
    telemetry=telemetry.fastapi_telemetry(),
    openapi_tags=TAGS,
    docs_url=None,    # Swagger UI loads a script from a CDN and runs an inline one: the page policy (script-src 'self') forbids both
    redoc_url=None,   # use /openapi.json with any OpenAPI viewer (docs/api.md)
)
_schema_without_marks = app.openapi


def _openapi_with_stable_marks():
    if app.openapi_schema is None:
        mark_stable(_schema_without_marks())
    return app.openapi_schema


app.openapi = _openapi_with_stable_marks  # operations of the documented API carry x-stable (docs/api.md)


# Order matters (last added = outermost): observability sees every request, including the ones
# the security layer rejects, so blocked requests show up in the metrics.
security_config = SecurityConfig.from_env()
app.add_middleware(ActivityMiddleware, tracker=activity.tracker)  # innermost: only requests security let through
# Level 5, not the default 9: on the largest responses it is ~2.5x faster for a few percent more bytes.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.add_middleware(SecurityMiddleware, cfg=security_config)
app.add_middleware(ObservabilityMiddleware)


STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def _asset_version(directory: Path = STATIC_DIR) -> str:
    """Content hash of the shipped static files (including subdirectories such as js/).

    Static files are served with a day-long Cache-Control, so the URL has to change whenever the
    content does (a version string alone would keep stale JS in browsers across redeploys). The hash
    is the /static/v/<hash>/ path prefix (served immutable)."""
    digest = hashlib.sha1()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()
app.state.asset_version = ASSET_VERSION  # GET /api/themes names the theme stylesheets by it
# Every module main.js imports or loads, directly or not (relative to static/): preloaded from the page head so the
# browser fetches them in one round instead of discovering them import level by import level.
JS_MODULES = [m for m in page_ui.modules(STATIC_DIR) if m != "js/main.js"]


def add_versioned_static(target: FastAPI, directory: Path, version: str) -> None:
    """Serve `directory` under /static/v/<version>/... (404 for any other version).

    ES modules import their siblings by relative URL, so loading the entry module from a
    hash-prefixed path makes every import inherit that prefix and get cache-busted with it.
    Must be registered before the plain /static mount, which would otherwise swallow the path."""
    files = page_ui.Assets(directory=str(directory))  # the panels' templates and manifests are never served

    @target.api_route("/static/v/{asset_version}/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def versioned_static(asset_version: str, asset_path: str, request: Request):
        if asset_version != version:
            raise HTTPException(status_code=404)
        sheet = themes.stylesheet_for(asset_path)  # a theme's stylesheet is generated from its theme.json, not a file
        if sheet is not None:
            return Response(sheet, media_type="text/css")
        return await files.get_response(asset_path, request.scope)


add_versioned_static(app, STATIC_DIR, ASSET_VERSION)
add_pwa_routes(app, STATIC_DIR, ASSET_VERSION)
app.mount("/static", page_ui.Assets(directory=str(STATIC_DIR)), name="static")
# The page template, and the panels' and dialogs' markup in their folders under static/ (penguwatch.ui)
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(STATIC_DIR)])
app.include_router(api_router)
app.include_router(metrics_router)
app.include_router(mcp_router)  # POST /mcp; 404 until mcp.enabled (docs/mcp.md)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard(request: Request):
    container = getattr(request.app.state, "container", None)  # None before startup (and in template tests)
    household = container.config.household if container else HouseholdConfig()
    display = container.config.display if container else DisplayConfig()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"version": ASSET_VERSION, "modules": JS_MODULES, "ui": page_ui.ui(), "grafana_links": grafana.links(),
                 "household": household, "household_json": json.dumps(household.labels()),
                 "looks": themes.page_list(ASSET_VERSION), "look_default": themes.resolve_default(display.theme)},
    )


def main():
    server = load_config().server  # config.yaml, with PENGUWATCH_HOST / PENGUWATCH_PORT applied on top
    host, port = server.host, server.port
    # The app object, not "penguwatch.main:app": under `python -m penguwatch.main` this module is __main__, and an
    # import string would make uvicorn import it a second time (every import-time step, and its log lines, twice)
    # proxy_headers=False: never take the client address from X-Forwarded-For (uvicorn trusts it from loopback by
    # default), so the sign-in throttle and the rate limits key on the real peer (docs/security.md)
    uvicorn.run(app, host=host, port=port, reload=False, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
