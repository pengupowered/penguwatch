"""Services tab: billing period, renewal date and note controls (static checks of the tab's module)."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static"
JS = (STATIC / "panels" / "services" / "panel.js").read_text(encoding="utf-8")
CSS = (STATIC / "panels" / "services" / "panel.css").read_text(encoding="utf-8")


def test_new_change_handlers_are_registered():
    for name in ("service-billing", "service-renewal-date", "service-note"):
        assert f'registerChange("{name}"' in JS
        assert f'data-change="{name}"' in JS


def test_existing_handlers_remain():
    assert 'registerChange("service-cost"' in JS and 'registerChange("service-renewal"' in JS
    assert 'registerAction("toggle-service"' in JS


def test_billing_select_offers_both_periods():
    assert '<option value="monthly"' in JS and '<option value="yearly"' in JS


def test_yearly_price_is_displayed_per_year_and_saved_monthly():
    assert "monthly_cost * 12" in JS
    assert "value / 12" in JS and "* 100) / 100" in JS
    assert "/ ${yearly ? \"year\" : \"month\"}" in JS


def test_yearly_uses_a_date_input_and_clearing_posts_null():
    assert 'type="date"' in JS
    assert 'el.value === "" ? null : el.value' in JS


def test_note_input_is_limited_and_has_placeholder():
    assert 'maxlength="120"' in JS
    assert "Note, e.g. included with your phone plan" in JS


def test_included_row_text_kept():
    assert "Included, no extra cost" in JS


def test_no_inline_handlers_in_services_module():
    assert not re.search(r"\son[a-z]+\s*=", JS)
    assert "javascript:" not in JS


def test_styles_exist():
    for cls in (".billing-select", ".note-input", ".svc-item-note"):
        assert cls in CSS
