"""Installable-app plumbing: web manifest, service worker and /favicon.ico.

Both must be reachable without the access token (browsers fetch the manifest without credentials, and
installing from the login page should work), so they are listed in security.PUBLIC_PATHS. They contain
no user data: the manifest is static and the worker only lists the versioned static shell."""

import json
from pathlib import Path

from fastapi import FastAPI, Response

from penguwatch import ui
from penguwatch.web import themes

SW_TEMPLATE = "sw.js"
MANIFEST = "manifest.webmanifest"


def shell_urls(directory: Path, version: str) -> list:
    """Everything the page needs to start, as the exact URLs index.html requests (versioned static only): the
    stylesheets, the theme script and the generated stylesheet of every theme (the one in use is linked by theme.js), and
    every module, the shared ones and each tab's and dialog's (penguwatch.ui)."""
    assets = (*ui.stylesheets(directory), "theme.js", *ui.modules(directory), "offline.css", "offline.html")
    urls = [f"/static/v/{version}/{path}" for path in assets]
    urls += [f"/static/v/{version}/themes/{theme_id}/theme.css" for theme_id in themes.look_stylesheets()]
    urls += [f"/static/icons/{p.name}" for p in sorted((directory / "icons").glob("*")) if p.is_file()]
    return urls


def render_service_worker(directory: Path, version: str) -> str:
    source = (directory / SW_TEMPLATE).read_text(encoding="utf-8")
    return (source.replace("__ASSET_VERSION__", version)
            .replace("__SHELL_URLS__", json.dumps(shell_urls(directory, version))))


def add_pwa_routes(target: FastAPI, directory: Path, version: str) -> None:
    worker = render_service_worker(directory, version)
    manifest = (directory / MANIFEST).read_text(encoding="utf-8")

    @target.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # no-cache: the browser must re-check the worker on every start so a new release is picked up
        return Response(worker, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    icon = (directory / "icons" / "favicon.ico").read_bytes()

    @target.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        # browsers ask for it on every page, the sign-in page included, so it is public (security.PUBLIC_PATHS)
        return Response(icon, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})

    @target.get("/manifest.webmanifest", include_in_schema=False)
    async def web_manifest():
        return Response(manifest, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})
