"""The ratings and webhooks UI: the tab is a complete folder, the modules read only what the API returns, every user
string is escaped, links are safe, and the batch size fits the API. (The scripts cannot run here, so how they behave
in a browser is checked in the offline browser kit, docs/ui.md; these tests keep the contract they rely on.)"""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import ui
from penguwatch.api.ratings import ImportResult, ShowRating
from penguwatch.api.webhooks import WebhookStatus
from penguwatch.main import app
from penguwatch.ratings import MAX_BATCH, RatingSource
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

STATIC = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static"
RATINGS = STATIC / "panels" / "ratings"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


PANEL = read(RATINGS / "panel.js")
CSV = read(RATINGS / "csv.js")
DETAILS = read(STATIC / "dialogs" / "details" / "dialog.js")
SYSTEM = read(STATIC / "panels" / "system" / "panel.js")
LIBRARY = read(STATIC / "panels" / "watchlist" / "panel.js")
UTIL = read(STATIC / "js" / "util.js")


def test_the_ratings_tab_is_a_complete_folder_on_the_more_sheet():
    part = next(p for p in ui.discover().panels if p.id == "ratings")
    assert (part.route, part.key, part.icon, part.bar) == ("ratings", "r", "star", False)
    assert {"panel.json", "panel.html", "panel.js", "panel.css", "source.html", "csv.js"} <= {p.name for p in RATINGS.iterdir()}


async def test_the_page_carries_the_ratings_markup(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        page = (await ac.get("/")).text
    for needle in ('id="ratings-tab"', 'id="rating-source-modal"', 'id="import-file"', 'id="import-source"',
                   'id="filter-minrating-source"', 'id="filter-minrating-value"', 'data-call="openRatingSource"'):
        assert needle in page, needle
    assert "/docs" not in page


def _reads(body: str, var: str) -> set:
    return set(re.findall(rf"\b{var}\.([a-z_]+)\b", body))


def test_modules_read_only_fields_the_api_returns():
    sources = set(RatingSource.model_fields)
    assert _reads(PANEL, "s") - sources - {"length"} == set()
    shown = set(ShowRating.model_fields)
    assert _reads(DETAILS[DETAILS.index("function renderShowRatings"):DETAILS.index("async function afterRatingChange")], "r") <= shown
    hooks = set(WebhookStatus.model_fields)
    assert _reads(SYSTEM[SYSTEM.index("function webhooksCard"):SYSTEM.index("function render(")], "h") <= hooks
    imported = set(ImportResult.model_fields)
    assert {"updated", "unchanged", "unmatched", "ambiguous"} <= imported and re.search(r"\bres\.updated\b", PANEL)
    assert _reads(PANEL[PANEL.index("async function runRatingImport"):], "res") <= imported


def test_the_system_tab_never_reads_an_address_or_a_secret():
    block = SYSTEM[SYSTEM.index("function webhooksCard"):SYSTEM.index("function render(")]
    assert not re.search(r"\.(url|secret)\b|secret_", block)
    assert "url" not in WebhookStatus.model_fields and "secret" not in WebhookStatus.model_fields


# Properties that hold text somebody typed or a third party sent: source names and labels, titles, links, errors
USER_TEXT = r"\.(name|label|title|url|host|last_error|disabled_reason|id|reason|message)\b"
SAFE = ("esc(", "Number(", ".toFixed", "encodeURIComponent(")


@pytest.mark.parametrize("name,body", [("ratings/panel.js", PANEL), ("details/dialog.js", DETAILS), ("system/panel.js", SYSTEM),
                                       ("js/util.js", UTIL)])
def test_every_user_string_put_into_markup_is_escaped(name, body):
    """Inside a template literal that builds markup, an interpolation of a user-text property goes through esc()."""
    bad = []
    for line_no, line in enumerate(body.splitlines(), 1):
        if "${" not in line or re.search(r"textContent|showToast\(|confirm\(|\.message\b|===", line):
            continue   # text nodes, toasts (escaped by showToast), dialogs and comparisons are not markup
        for expr in re.findall(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}", line):
            if re.search(USER_TEXT, expr) and not any(tok in expr for tok in SAFE):
                bad.append(f"{name}:{line_no}: ${{{expr}}}")
    # a few known-safe sites: ids already escaped on the line, data from our own tables
    allowed = [b for b in bad if re.search(r"STATUS_LABELS|MEDIA|HOOK_STATE|displayTitle|kind\b|\.dataset\.|show\.id|\.state\b", b)]
    assert [b for b in bad if b not in allowed] == []


def test_options_are_filled_as_text_not_markup():
    assert "o.textContent = text" in LIBRARY and "o.textContent = `${s.name}" in PANEL
    assert "innerHTML" not in LIBRARY[LIBRARY.index("function fillRatingControls"):LIBRARY.index("// Poster cards vs. a compact list")]


def test_links_the_rating_ui_makes_open_safely_and_only_to_http():
    assert 'target="_blank" rel="noopener noreferrer"' in DETAILS
    assert not re.search(r'target="_blank"(?![^>]*noopener noreferrer)', DETAILS[DETAILS.index("function renderShowRatings"):DETAILS.index("async function afterRatingChange")])
    assert re.search(r"safeLink = \(url\) => \(/\^https\?:", DETAILS)
    for block in (PANEL, SYSTEM[SYSTEM.index("function webhooksCard"):SYSTEM.index("function render(")]):
        assert "<a " not in block   # nothing user-supplied becomes a link on these screens


def test_the_import_sends_batches_the_api_accepts():
    batch = int(re.search(r"const BATCH = (\d+)", PANEL).group(1))
    assert batch <= MAX_BATCH and batch * 400 < 256 * 1024      # a typical item of about 400 bytes fits the body cap
    assert "/api/ratings/${encodeURIComponent(source)}" in PANEL and 'method: "PUT"' in PANEL


def test_the_csv_download_neutralises_formulas_and_keeps_plain_numbers():
    assert r"/^[=+\-@\t\r]/" in CSV and "'${s}" in CSV and r"!/^-?\d+([.,]\d+)?$/" in CSV


def test_the_library_uses_the_apis_sort_and_filter_names(setup_test_app):
    assert "`rating:${s.id}`" in LIBRARY and 'params.append("min_rating", `${f.minSource}:${f.minValue}`)' in LIBRARY


async def test_the_api_accepts_what_the_library_sends(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post("/api/rating-sources", json={"id": "lb", "name": "LB", "label": "LB", "scale_max": 5})
        ok = await ac.get("/api/shows", params={"view": "card", "sort_by": "rating:lb", "min_rating": "lb:3.5"})
        assert ok.status_code == 200
        details = await ac.get("/api/shows/1/ratings")
        assert details.status_code == 404      # no such title; the shape is checked in test_ratings


async def test_the_ratings_endpoints_the_ui_calls_exist():
    paths = app.openapi()["paths"]
    calls = re.findall(r'[`"](/api/(?:rating-sources|ratings|webhooks)[^`"]*|/api/shows/\$\{[^}]*\}/ratings[^`"]*)[`"]',
                       PANEL + DETAILS + SYSTEM)
    assert len(calls) >= 6
    for call in calls:
        concrete = re.sub(r"\$\{[^}]*\}", "x", call)
        assert any(re.fullmatch(re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(p)), concrete) for p in paths), call
