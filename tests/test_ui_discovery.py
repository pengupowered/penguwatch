"""penguwatch.ui: the tab and dialog folders are found at start, in their order, and their templates are not files."""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from penguwatch import ui


def _panel(root, name, order, **extra):
    folder = root / "panels" / name
    folder.mkdir(parents=True)
    (folder / "panel.json").write_text(json.dumps({"order": order, "title": name.title(), "icon": "tv", **extra}))
    (folder / "panel.html").write_text(f'<section id="{name}-tab" data-panel="{name}"></section>')
    (folder / "panel.js").write_text("export default {};")
    (folder / "panel.css").write_text("")
    return folder


def test_panels_are_found_in_their_order_with_their_dialogs_and_styles(tmp_path):
    _panel(tmp_path, "second", 20, route="two", key="t", bar=True)
    first = _panel(tmp_path, "first", 10)
    (first / "help.html").write_text("<div class='modal' id='help'></div>")
    (first / "extra.css").write_text("")
    shared = tmp_path / "dialogs" / "shared"
    shared.mkdir(parents=True)
    (shared / "dialog.html").write_text("<div class='modal' id='shared'></div>")
    (shared / "dialog.js").write_text("export default {};")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "main.js").write_text("")
    found = ui.discover(tmp_path)
    assert [p.id for p in found.panels] == ["first", "second"]
    one, two = found.panels
    assert one.route == "first" and one.short == "First" and not one.bar and one.tab == "first-tab"
    assert two.route == "two" and two.key == "t" and two.bar
    assert one.dialogs == ("panels/first/help.html",)
    assert one.css == ("panels/first/extra.css", "panels/first/panel.css")
    assert found.dialog_templates == ("panels/first/help.html", "dialogs/shared/dialog.html")
    assert found.modules == {"dialogs": ["dialogs/shared/dialog.js"], "panels": ["panels/first/panel.js", "panels/second/panel.js"]}
    assert ui.modules(tmp_path) == ("js/main.js", "panels/first/panel.js", "panels/second/panel.js", "dialogs/shared/dialog.js")
    assert ui.stylesheets(tmp_path)[:4] == ("tokens.css", "components.css", "page.css", "style.css")


def test_a_tree_without_folders_has_no_tabs(tmp_path):
    found = ui.discover(tmp_path)
    assert found.panels == () and found.dialogs == () and found.css == ()


@pytest.mark.parametrize("path,refused", [
    ("panels/first/panel.html", True), ("panels/first/panel.json", True), ("dialogs/shared/dialog.html", True),
    ("panels/./first/panel.html", True), ("panels\\first\\panel.html", True), ("/panels/first/PANEL.HTML", True),
    ("panels/first/panel.js", False), ("panels/first/panel.css", False), ("offline.html", False), ("js/main.js", False),
])
def test_templates_and_manifests_are_recognised(path, refused):
    assert ui.is_template(path) is refused


@pytest.mark.asyncio
async def test_the_assets_app_does_not_serve_them(tmp_path):
    _panel(tmp_path, "first", 10)
    target = FastAPI()
    target.mount("/static", ui.Assets(directory=str(tmp_path)), name="static")
    async with AsyncClient(transport=ASGITransport(app=target), base_url="http://test") as ac:
        assert (await ac.get("/static/panels/first/panel.js")).status_code == 200
        for path in ("panels/first/panel.html", "panels/first/panel.json"):
            assert (await ac.get(f"/static/{path}")).status_code == 404
