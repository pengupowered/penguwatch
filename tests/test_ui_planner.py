"""The rotation planner section exists on the Optimizer tab and its module is wired in."""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app

OPTIMIZER = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static" / "panels" / "optimizer"


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


@pytest.mark.asyncio
async def test_planner_section_is_first_inside_the_optimizer_tab():
    html = await _index()
    tab = html.index('id="optimizer-tab"')
    planner = html.index('id="planner-section"')
    assert tab < planner < html.index('class="optimizer-hero"')


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", [
    "planner-months", "planner-summary", "planner-grid", "planner-table-view",
    "planner-table-wrap", "planner-why", "planner-notes", "planner-status",
])
async def test_planner_controls_exist(element_id):
    assert f'id="{element_id}"' in await _index()


@pytest.mark.asyncio
async def test_months_selector_offers_3_6_and_12_through_data_change():
    select = re.search(r'<select id="planner-months"[^>]*>(.*?)</select>', await _index(), re.S)
    assert select is not None
    assert 'data-change="planner-months"' in select.group(0)
    assert re.findall(r'<option value="(\d+)"', select.group(1)) == ["3", "6", "12"]


def test_the_optimizer_tab_imports_the_planner_module():
    assert 'import "./planner.js"' in (OPTIMIZER / "panel.js").read_text(encoding="utf-8")


def test_planner_module_registers_through_dispatch_and_tabs():
    src = (OPTIMIZER / "planner.js").read_text(encoding="utf-8")
    assert 'onTabActivated("optimizer-tab", loadPlanner)' in src
    assert 'registerChange("planner-months"' in src
    assert 'from "../../js/dispatch.js"' in src


def test_planner_module_has_no_inline_handlers_and_stays_small():
    src = (OPTIMIZER / "planner.js").read_text(encoding="utf-8")
    assert not re.search(r"\bon[a-z]+\s*=\s*[\"']", src)
    assert len(src.splitlines()) < 400
