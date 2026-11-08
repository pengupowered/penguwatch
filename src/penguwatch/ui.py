"""The page's tabs (panels) and shared dialogs, discovered from their folders at start.

Every tab lives in its own folder, web/static/panels/<id>/, with
  panel.json   its manifest: order (its place in the tab bar), title, short (the phone label), icon (a sprite name),
               route (the #/<route> address), key (the "g <key>" shortcut) and bar (true: on the phone's bottom bar)
  panel.html   its markup: exactly one <section id="<id>-tab" data-panel="<id>">
  panel.js     its module (default export {id, mount()})
  *.css        its styles; any other *.html is a dialog the tab owns (included outside <main>)
and every dialog that no tab owns (opened from several tabs or from the header) lives in web/static/dialogs/<id>/
with dialog.html, dialog.js and *.css. The page includes, links and loads whatever is there, so adding a tab is adding
a folder: no shared file names it. Folders live under static/ so a module's relative imports mean the same on disk
and in the URL; their .html and .json are templates and manifests, never served as files (main.py refuses them).
"""

import json
import posixpath
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
NOT_SERVED = (".html", ".json")  # under panels/ and dialogs/: templates and manifests
PART_ROOTS = ("panels", "dialogs")


@dataclass(frozen=True)
class Part:
    """One panel or dialog folder. Paths are relative to web/static (template names and asset URLs alike)."""

    id: str
    order: float
    template: str  # the panel's markup, or the dialog's ("" for a dialog without markup)
    dialogs: Tuple[str, ...]  # a panel's own dialog templates
    css: Tuple[str, ...]
    module: str  # "" when the folder has no module
    title: str = ""
    short: str = ""
    icon: str = ""
    route: str = ""
    key: str = ""
    bar: bool = False

    @property
    def tab(self) -> str:
        """The id of the panel's <section> (and of its tab button's target)."""
        return f"{self.id}-tab"


@dataclass(frozen=True)
class Ui:
    panels: Tuple[Part, ...]
    dialogs: Tuple[Part, ...]

    @property
    def css(self) -> Tuple[str, ...]:
        return tuple(c for p in (*self.panels, *self.dialogs) for c in p.css)

    @property
    def dialog_templates(self) -> Tuple[str, ...]:
        return tuple(t for p in self.panels for t in p.dialogs) + tuple(d.template for d in self.dialogs if d.template)

    @property
    def modules(self) -> dict:
        """What main.js loads (body data-modules): the shared dialogs' modules, then the panels' in tab order."""
        return {"dialogs": [d.module for d in self.dialogs if d.module], "panels": [p.module for p in self.panels]}


def _rel(path: Path, static: Path) -> str:
    return path.relative_to(static).as_posix()


def _folders(root: Path):
    return sorted(f for f in root.iterdir() if f.is_dir()) if root.is_dir() else []


def discover(static: Path = STATIC_DIR) -> Ui:
    panels = []
    for folder in _folders(static / "panels"):
        manifest = json.loads((folder / "panel.json").read_text(encoding="utf-8"))
        panels.append(Part(
            id=folder.name, order=float(manifest.get("order", 1000)),
            template=_rel(folder / "panel.html", static),
            dialogs=tuple(_rel(p, static) for p in sorted(folder.glob("*.html")) if p.name != "panel.html"),
            css=tuple(_rel(p, static) for p in sorted(folder.glob("*.css"))),
            module=_rel(folder / "panel.js", static),
            title=manifest["title"], short=manifest.get("short", manifest["title"]), icon=manifest["icon"],
            route=manifest.get("route", folder.name), key=manifest.get("key", ""), bar=bool(manifest.get("bar"))))
    panels.sort(key=lambda p: (p.order, p.id))
    dialogs = []
    for folder in _folders(static / "dialogs"):
        dialogs.append(Part(
            id=folder.name, order=0,
            template=_rel(folder / "dialog.html", static) if (folder / "dialog.html").is_file() else "",
            dialogs=(), css=tuple(_rel(p, static) for p in sorted(folder.glob("*.css"))),
            module=_rel(folder / "dialog.js", static) if (folder / "dialog.js").is_file() else ""))
    return Ui(panels=tuple(panels), dialogs=tuple(dialogs))


@lru_cache(maxsize=1)
def ui() -> Ui:
    return discover()


def modules(static: Path = STATIC_DIR) -> Tuple[str, ...]:
    """Every ES module the page runs (the shared js/ ones and every folder's), relative to static/: what the page
    preloads and the service worker keeps. main.js imports js/ itself and loads the folders' modules."""
    found = [*sorted((static / "js").glob("*.js"))]
    for root in PART_ROOTS:
        found += sorted((static / root).rglob("*.js"))
    return tuple(_rel(p, static) for p in found)


def stylesheets(static: Path = STATIC_DIR) -> Tuple[str, ...]:
    """The shared stylesheets in the order the page links them, then every folder's (relative to static/)."""
    shared = ("tokens.css", "components.css", "page.css", "style.css")
    return shared + discover(static).css


def is_template(asset_path: str) -> bool:
    """A panel's or dialog's markup or manifest: rendered into the page, never served as a file."""
    path = posixpath.normpath(asset_path.replace("\\", "/")).lstrip("/")
    return path.split("/")[0] in PART_ROOTS and path.lower().endswith(NOT_SERVED)


class Assets(StaticFiles):
    """The static files, except the folders' templates and manifests (is_template), which are a 404."""

    async def get_response(self, path: str, scope):
        if is_template(path):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)
