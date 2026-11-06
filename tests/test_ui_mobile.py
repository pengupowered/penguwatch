"""Mobile layout: markup hooks, the More sheet module, CSS care points and the covered-member card."""

import re
import tomllib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "penguwatch" / "web" / "static"
JS = STATIC / "js"


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", ["nav-more", "more-sheet", "more-list", "more-backdrop", "upnext-chips",
                                        "header-line", "header-review", "header-sync"])
async def test_template_has_the_mobile_hooks(element_id):
    assert f'id="{element_id}"' in await _index()


@pytest.mark.asyncio
async def test_more_sheet_is_a_modal_dialog_and_button_is_not_a_nav_tab():
    html = await _index()
    sheet = re.search(r'<div class="more-sheet"[^>]*>', html)
    assert sheet and 'role="dialog"' in sheet.group(0) and 'aria-modal="true"' in sheet.group(0)
    more = re.search(r'<button[^>]*id="nav-more"[^>]*>', html).group(0)
    assert "nav-btn" not in more and "data-tab" not in more   # tabs.js must not wire it as a tab
    # every tab button has its full label and the short one the phone's bar shows; the Optimizer's is "Plan"
    plan = re.search(r'<button[^>]*data-tab="optimizer-tab"[^>]*>(.*?)</button>', html).group(1)
    assert '<span class="nav-full">Optimizer</span>' in plan and '<span class="nav-short" aria-hidden="true">Plan</span>' in plan
    # the phone's bottom bar: the tabs marked for it (panel.json "bar") and More
    assert re.findall(r'data-tab="(\w+)-tab"[^>]*data-bar', html) == ["upnext", "optimizer", "watchlist"]


def test_main_imports_mobile_module_and_it_has_no_inline_handlers():
    main = (JS / "main.js").read_text(encoding="utf-8")
    assert 'from "./mobile.js"' in main and "setupMobile()" in main
    src = (JS / "mobile.js").read_text(encoding="utf-8")
    assert not re.search(r"on(click|change|input|keydown)s*=", src)
    assert "Escape" in src and "aria-expanded" in src
    assert len(src.splitlines()) < 400


def test_package_data_covers_mobile_js():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = data["tool"]["setuptools"]["package-data"]["penguwatch"]
    assert "web/static/**/*" in globs and (JS / "mobile.js").is_file()


def test_css_mobile_block_has_the_care_points():
    css = (STATIC / "page.css").read_text(encoding="utf-8")
    block = css[css.rindex("Mobile chrome"):]
    assert "@media (max-width: 720px)" in block
    assert "safe-area-inset-bottom" in block
    assert "prefers-reduced-motion" in block
    assert "min-height: 48px" in block  # the bottom bar's touch targets
    upnext = (STATIC / "panels" / "upnext" / "panel.css").read_text(encoding="utf-8")
    assert "@media (max-width: 720px)" in upnext and "width: 46px" in upnext  # the round "watched" button


def test_up_next_chips_use_the_dispatcher_not_inline_handlers():
    src = (STATIC / "panels" / "upnext" / "panel.js").read_text(encoding="utf-8")
    assert 'registerAction("upnext-chip"' in src
    assert 'data-action="upnext-chip"' in src
    assert not re.search(r"\sonclick=", src)


def test_covered_member_card_says_included_and_has_no_toggle():
    src = (STATIC / "panels" / "optimizer" / "panel.js").read_text(encoding="utf-8")
    assert "Included in " in src
    assert re.search(r"if \(rec\.covered_by\) return \"\";", src)
