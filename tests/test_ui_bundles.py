"""The services/bundles/interest controls exist in the template and the new module is wired in."""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app

SERVICES = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static" / "panels" / "services"


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", [
    "add-service-form", "add-service-name", "add-service-price", "add-service-aliases", "add-service-titles",
    "add-service-edit-id", "btn-cancel-edit-service",
    "add-service-is-bundle", "add-service-members", "add-service-error", "bundle-suggestions",
])
async def test_template_has_the_new_controls(element_id):
    assert f'id="{element_id}"' in await _index()


@pytest.mark.asyncio
async def test_interest_sort_option_is_in_the_library_sort_select():
    select = re.search(r'<select id="sort-by-select".*?</select>', await _index(), re.S)
    assert select is not None
    assert '<option value="interest">Likely interest' in select.group(0)


def test_addservice_module_exists_and_is_started_by_the_services_tab():
    assert (SERVICES / "addservice.js").is_file()
    panel = (SERVICES / "panel.js").read_text(encoding="utf-8")
    assert 'from "./addservice.js"' in panel
    assert "setupAddService()" in panel


def test_edit_on_a_service_row_fills_the_add_form_and_posts_to_the_same_route():
    panel = (SERVICES / "panel.js").read_text(encoding="utf-8")
    add = (SERVICES / "addservice.js").read_text(encoding="utf-8")
    assert 'registerAction("service-edit"' in panel and 'data-action="service-edit"' in panel
    assert "export function startEditService" in add
    assert add.count('api("/api/services", { method: "POST"') == 1  # one save path for add and edit
    assert "titles: f.titles" in add


@pytest.mark.asyncio
async def test_services_text_points_to_settings_not_config_yaml():
    text = (SERVICES / "panel.html").read_text(encoding="utf-8")
    assert "Settings &gt; Data sources" in text
    assert "config.yaml" not in text


def test_ratings_tab_has_no_config_yaml_badge():
    panel = (SERVICES.parent / "ratings" / "panel.js").read_text(encoding="utf-8")
    assert "config.yaml" not in panel
