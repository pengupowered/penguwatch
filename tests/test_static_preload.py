"""The page loads every static asset from its immutable hash-prefixed URL and preloads all JS modules."""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import ui
from penguwatch.main import ASSET_VERSION, STATIC_DIR, app


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/")
    assert res.status_code == 200
    return res.text


@pytest.mark.asyncio
async def test_page_assets_use_the_versioned_prefix():
    html = await _index()
    assert "?v=" not in html
    for asset in ("tokens.css", "components.css", "page.css", "style.css", "theme.js", "js/main.js",
                  "panels/watchlist/panel.css", "dialogs/details/dialog.css"):
        assert f"/static/v/{ASSET_VERSION}/{asset}" in html
    # nothing under /static is linked without the hash (the icons are not versioned on purpose: the manifest names them)
    unversioned = set(re.findall(r'(?:href|src)="/static/(?!v/|icons/)([^"]+)"', html))
    assert unversioned == set()


@pytest.mark.asyncio
async def test_every_imported_module_is_preloaded():
    html = await _index()
    preloaded = set(re.findall(rf'rel="modulepreload" href="/static/v/{ASSET_VERSION}/([^"]+)"', html))
    on_disk = {p.relative_to(STATIC_DIR).as_posix() for root in ("js", "panels", "dialogs")
               for p in (STATIC_DIR / root).rglob("*.js")} - {"js/main.js"}
    assert preloaded == on_disk
    assert set(ui.modules()) - {"js/main.js"} == on_disk
