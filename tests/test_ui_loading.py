"""Loading feedback and slow-request reporting in the page scripts (static checks; the browser runs them)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "penguwatch" / "web" / "static"
JS = STATIC / "js"


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def _panel(tab: str) -> str:
    return (STATIC / "panels" / tab / "panel.js").read_text(encoding="utf-8")


def test_api_wrapper_reports_slow_requests_but_never_its_own_reports():
    api = _read("api.js")
    assert "const SLOW_MS = 2000;" in api
    assert '"/api/client-timing"' in api
    assert "pathname === REPORT_PATH" in api  # a slow report must not report itself
    assert "keepalive: true" in api
    assert "server-timing" in api  # server time comes from our Server-Timing header
    assert "resourcetimingbufferfull" in api  # a page left open for hours keeps getting phase timings


def test_views_show_a_placeholder_and_replace_it_on_failure():
    assert 'showLoading(container, "Loading your next episodes…")' in _panel("upnext")
    library, optimizer = _panel("watchlist"), _panel("optimizer")
    assert "showLoading(gridEl()" in library and "showLoadError(gridEl(), err)" in library
    assert 'showLoading(document.getElementById("optimizer-rankings-container")' in optimizer
    assert "showLoadError(document.getElementById(\"optimizer-rankings-container\"), err)" in optimizer


def test_pill_styles_exist():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for rule in (".net-status {", ".net-status.visible", ".net-status.slow", ".loading-state"):
        assert rule in css
