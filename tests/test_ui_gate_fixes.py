"""Static checks for the browser-gate fixes (planner notes and cells, header sync age, mobile overflow)."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static"


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def _func(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    return src[start:src.index("\nfunction ", start + 1) if "\nfunction " in src[start + 1:] else len(src)]


def test_pause_icon_is_a_common_glyph():
    assert "\u23f8" not in _read("panels/optimizer/planner.js")
    assert "\u23f8" not in _read("panels/optimizer/panel.css")
    assert 'pause: { icon: "pause"' in _read("panels/optimizer/planner.js")  # a sprite icon, not a glyph


def test_planner_notes_show_unassigned_once():
    body = _func(_read("panels/optimizer/planner.js"), "renderNotes")
    assert "titles have no service assigned." not in body
    assert body.count("planner-tidy-link") == 1
    assert "plan.notes.map" in body


def test_planner_cells_do_not_break_words_and_have_title():
    js = _read("panels/optimizer/planner.js")
    assert 'class="planner-seg planner-${esc(seg.kind)}" title="${esc(segText(seg))}"' in js
    css = _read("panels/optimizer/panel.css")
    assert re.search(r"\.planner-seg span:last-child\s*\{[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap", css)


def test_header_sync_shows_age_before_not_connected():
    js = _read("js/header.js")
    assert "last_sync ? syncAge(st.last_sync)" in js
    assert "min ago" in js and " h ago" in js and " d ago" in js
    assert js.index("syncAge(st.last_sync)") < js.index("not connected")


def test_css_overflow_rules():
    assert re.search(r"\.sys-grid\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)", _read("panels/system/panel.css"))
    assert re.search(r"\.status-filters\s*\{[^}]*overflow-x:\s*auto[^}]*flex-wrap:\s*nowrap", _read("style.css"))
    library = _read("panels/watchlist/panel.css")
    assert "tidy-assign-row" in library and "minmax(10rem, 1fr)" in library


def test_header_sync_age_has_no_prefix_and_full_title():
    js = _read("js/header.js")
    assert "`synced" not in js and "\"synced" not in js
    assert "sync ${age}" in js and "aria-label" in js
