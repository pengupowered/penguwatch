"""Optimizer screen: one status per service, plan and raw numbers collapsed (static checks)."""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app

STATIC = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static"
OPT = (STATIC / "panels" / "optimizer" / "panel.js").read_text(encoding="utf-8")
PLANNER = (STATIC / "panels" / "optimizer" / "planner.js").read_text(encoding="utf-8")
CSS = (STATIC / "panels" / "optimizer" / "panel.css").read_text(encoding="utf-8")


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", ["opt-summary", "opt-subscriptions", "opt-next-details", "opt-plan-details", "opt-raw-details"])
async def test_new_regions_exist(element_id):
    assert f'id="{element_id}"' in await _index()


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", ["opt-next-details", "opt-plan-details", "opt-raw-details"])
async def test_details_are_collapsed_by_default(element_id):
    tag = re.search(rf'<details[^>]*id="{element_id}"[^>]*>', await _index())
    assert tag is not None
    assert " open" not in tag.group(0)


@pytest.mark.asyncio
async def test_regions_appear_in_the_target_order():
    html = await _index()
    order = [html.index(f'id="{i}"') for i in
             ("opt-summary", "opt-subscriptions", "opt-next-details", "opt-plan-details", "opt-raw-details")]
    assert order == sorted(order)


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", [
    "planner-section", "bundle-suggestions", "optimizer-rankings-container", "rotation-advice-box",
    "value-report", "opt-unused", "opt-active-count", "opt-monthly-spend", "head-unused",
])
async def test_existing_ids_survive(element_id):
    assert f'id="{element_id}"' in await _index()


@pytest.mark.asyncio
async def test_planner_and_ranking_cards_sit_inside_their_details():
    html = await _index()
    plan = html[html.index('id="opt-plan-details"'):html.index('id="opt-raw-details"')]
    assert 'id="planner-section"' in plan
    start = html.index('id="opt-raw-details"')
    raw = html[start:html.index("data-panel=", start)]  # up to the next tab's section
    for needed in ("rotation-advice-box", "value-report", "opt-unused", "opt-active-count"):
        assert needed in raw


def test_status_order_is_most_urgent_first():
    m = re.search(r"export const STATUS_ORDER = \[([^\]]*)\]", OPT)
    assert m is not None
    order = re.findall(r'"([a-z_]+)"', m.group(1))
    assert order == ["consider_cancel", "decide_soon", "check", "watching", "in_use", "paid_through", "included", "covered"]
    assert "export function sortByStatus" in OPT


def test_only_actionable_rows_get_the_toggle():
    assert 'rec.status === "consider_cancel" || rec.status === "decide_soon"' in OPT
    assert re.search(r"if \(rec\.covered_by\) return \"\";", OPT)
    assert 'if (rec.is_free) return "";' in OPT


def test_header_pill_uses_savable_cost_with_fallback():
    assert "plan.savable_monthly_cost !== undefined ? plan.savable_monthly_cost : plan.unused_monthly_cost" in OPT


def test_ranking_cards_list_only_inactive_services():
    assert "filter((rec) => !rec.is_active && !rec.covered_by)" in OPT


def test_status_chips_carry_an_icon_and_a_word():
    assert '<span class="badge ${chip.cls}">${icon(chip.icon)} ${chip.label}</span>' in OPT
    # every status and verdict names a sprite icon, a word and a status colour
    assert len(re.findall(r'icon: "\w+", label: "[^"]+", cls: "st-', OPT)) >= 13


def test_planner_folds_renewal_day_notes():
    assert "export function foldRenewalNotes" in PLANNER
    assert "Renewal day unknown for: " in PLANNER
    assert "(set it on the Services tab)" in PLANNER
    assert "foldRenewalNotes(plan.notes)" in PLANNER


def test_no_inline_handlers_and_size_budget():
    for src in (OPT, PLANNER):
        assert not re.search(r"\bon[a-z]+\s*=\s*[\"']", src)
        assert len(src.splitlines()) < 400


def test_styles_exist():
    for cls in (".opt-summary", ".opt-sum-card", ".opt-table", ".opt-details"):
        assert cls in CSS
