"""App-wide reconnect banner, renewal status line and the auth fields of /api/drivers/simkl/status."""

import re
import time
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app
from penguwatch.drivers.simkl import auth as auth_state

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "penguwatch" / "web" / "static"
JS = STATIC / "js"
LIBRARY_JS = STATIC / "panels" / "services" / "library.js"
SETTINGS_URL = "https://api.simkl.com/users/settings"


async def _index() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.get("/")).text


@pytest.mark.asyncio
@pytest.mark.parametrize("element_id", ["auth-banner", "auth-banner-text",
                                        "btn-auth-banner-reconnect", "library-token-renewal"])
async def test_template_has_auth_hooks(element_id):
    assert f'id="{element_id}"' in await _index()


@pytest.mark.asyncio
async def test_banner_is_alert_hidden_and_uses_dispatch():
    html = await _index()
    banner = re.search(r'<div class="auth-banner[ "][^>]*>', html).group(0)
    assert 'role="alert"' in banner and " hidden" in banner
    assert html.index("</header>") < html.index('id="auth-banner"') < html.index("<main")
    button = re.search(r'<button[^>]*id="btn-auth-banner-reconnect"[^>]*>', html).group(0)
    assert 'data-call="startReconnect"' in button and "onclick" not in button


def test_library_js_registers_reconnect_and_the_banner_shows_on_every_check():
    src = LIBRARY_JS.read_text(encoding="utf-8")
    assert 'from "../../js/dispatch.js"' in src and "startReconnect" in src
    assert "Renews automatically" in src
    assert not re.search(r"on(click|change|input|keydown)s*=", src)
    # the app-wide banner is the header's: drawn on every status check, a light one (the poll) included
    header = (JS / "header.js").read_text(encoding="utf-8")
    handler = re.search(r'on\("library-status", \(\{ data \}\) => \{([^}]*)\}\);', header)
    assert handler and "renderAuthBanner(data)" in handler.group(1) and "light" not in handler.group(1)
    status = (JS / "library-status.js").read_text(encoding="utf-8")
    assert 'emit("library-status", { data, light })' in status


def test_mobile_header_mentions_expired_login():
    src = (JS / "header.js").read_text(encoding="utf-8")
    assert 'st.auth_state === "needs_reconnect"' in src and "login expired" in src


def test_css_auth_banner_rules():
    css = (STATIC / "page.css").read_text(encoding="utf-8")
    assert ".auth-banner[hidden]" in css and "display: none !important" in css
    rule = re.search(r"\.auth-banner \{[^}]*\}", css).group(0)
    assert "var(--st-bg)" in rule and "var(--st)" in rule and "flex-wrap: wrap" in rule  # the status colour it is given
    phone = css[css.index("@media (max-width: 720px)"):]
    assert re.search(r"\.auth-banner \{ margin-inline: 12px; width: auto; \}", phone)
    header = (JS / "header.js").read_text(encoding="utf-8")
    assert 'banner.className = `auth-banner ${needs ? "st-crit" : "st-warn"}`' in header  # expired: red, expiring: amber


async def _status(container):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/drivers/simkl/status")
    assert res.status_code == 200
    return res.json()


@pytest.mark.asyncio
@respx.mock
async def test_status_auth_state_ok(container):
    respx.post(SETTINGS_URL).respond(json={})
    container.drivers.get("simkl").client.set_tokens("mock_token", "ref", time.time() + 5 * 86400)
    await auth_state.persist_tokens(container.repository, "mock_token", "ref", time.time() + 5 * 86400)
    data = await _status(container)
    assert data["auth_state"] == "ok" and data["can_auto_renew"] is True
    assert data["auth_message"] is None
    assert data["token_expires_at"] and data["token_renewed_at"]


@pytest.mark.asyncio
@respx.mock
async def test_status_needs_reconnect(container):
    respx.post(SETTINGS_URL).respond(json={})
    container.drivers.get("simkl").client.set_tokens("mock_token", "ref", None)
    container.drivers.get("simkl").client.refresh_rejected = True
    await auth_state.mark_needs_reconnect(container.repository)
    data = await _status(container)
    assert data["auth_state"] == "needs_reconnect"
    assert "Reconnect SIMKL" in data["auth_message"]
    assert data["can_auto_renew"] is False


@pytest.mark.asyncio
@respx.mock
async def test_status_rejected_renewal_is_a_note_not_the_banner(container):
    respx.post(SETTINGS_URL).respond(json={})
    container.drivers.get("simkl").client.set_tokens("mock_token", "ref", time.time() + 5 * 86400)
    container.drivers.get("simkl").client.refresh_rejected = True
    await auth_state.clear_needs_reconnect(container.repository)
    data = await _status(container)
    assert data["auth_state"] == "ok" and data["can_auto_renew"] is False
    assert data["renewal_note"].startswith("Automatic renewal failed; reconnect before the login expires on ")
    # once an API call really fails, the banner takes over and the note goes away
    await auth_state.mark_needs_reconnect(container.repository)
    data = await _status(container)
    assert data["auth_state"] == "needs_reconnect" and data["renewal_note"] is None


@pytest.mark.asyncio
@respx.mock
async def test_status_has_no_note_when_renewal_works(container):
    respx.post(SETTINGS_URL).respond(json={})
    container.drivers.get("simkl").client.set_tokens("mock_token", "ref", time.time() + 5 * 86400)
    assert (await _status(container))["renewal_note"] is None


def test_library_js_words_the_login_line_by_refresh_token_not_by_how_it_was_made():
    src = LIBRARY_JS.read_text(encoding="utf-8")
    assert "login can't renew automatically" in src
    assert "Manual token" not in src  # PIN logins without a refresh token are not "manual"


def test_library_js_shows_the_renewal_note_on_services_and_sync():
    src = LIBRARY_JS.read_text(encoding="utf-8")
    assert "library-renewal-note" in src and "data.renewal_note" in src


@pytest.mark.asyncio
async def test_template_has_renewal_note_outside_the_banner():
    html = await _index()
    assert 'id="library-renewal-note"' in html
    assert html.index('id="library-renewal-note"') > html.index("<main")


@pytest.mark.asyncio
async def test_status_disconnected(container):
    container.drivers.get("simkl").client.set_tokens(None, None, None)
    data = await _status(container)
    assert data["auth_state"] == "disconnected" and data["authenticated"] is False
    assert data["can_auto_renew"] is False and data["token_expires_at"] is None


@pytest.mark.asyncio
@respx.mock
async def test_status_expiring_without_refresh_token(container):
    respx.post(SETTINGS_URL).respond(json={})
    container.drivers.get("simkl").client.refresh_token = None
    container.drivers.get("simkl").client.set_tokens("mock_token", None, time.time() + 3600)
    data = await _status(container)
    assert data["auth_state"] == "expiring" and data["can_auto_renew"] is False
    # a manual token with unknown expiry is not flagged
    container.drivers.get("simkl").client.set_tokens("mock_token", None, None)
    assert (await _status(container))["auth_state"] == "ok"
