"""Library frontend contract: the slim card view feeds the card renderer; no per-card <select>s."""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.api.shows import CARD_FIELDS
from penguwatch.main import app
from penguwatch.models import Show, ShowStatus
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

STATIC = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static"
JS = STATIC / "js"
LIBRARY = STATIC / "panels" / "watchlist"

# Everything cards.js and util.js (ratingBadges) read from a list item
CARD_READS = [
    "id", "ids", "media_type", "title", "year", "poster", "network", "service_id", "service_manual",
    "status", "priority", "household", "simkl_rating", "imdb_rating", "user_rating",
    "total_episodes", "watched_episodes", "aired_episodes", "next_to_watch", "last_watched", "is_ready_to_binge",
]


@pytest.mark.asyncio
async def test_card_view_has_every_field_the_card_renderer_reads(setup_test_app):
    repo = setup_test_app
    await repo.upsert_show(Show(title="Wide", overview="long text " * 200, status=ShowStatus.WATCHING, aired_episodes=8))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        card = (await ac.get("/api/shows?view=card")).json()[0]
    assert "overview" not in card
    assert [f for f in CARD_READS if f not in card] == []


def test_card_renderer_only_reads_fields_the_card_view_returns():
    cards = (LIBRARY / "cards.js").read_text(encoding="utf-8")
    util = (JS / "util.js").read_text(encoding="utf-8")   # ratingBadges takes a show
    read = set(re.findall(r"\b(?:show|s)\.([a-z_]+)", cards)) | set(re.findall(r"\bshow\.([a-z_]+)", util))
    assert read - CARD_FIELDS == set()


def test_library_fetches_the_card_view_and_cards_have_no_selects():
    assert 'new URLSearchParams({ view: "card" })' in (LIBRARY / "panel.js").read_text(encoding="utf-8")
    cards = (LIBRARY / "cards.js").read_text(encoding="utf-8")
    assert "<select" not in cards and "<option" not in cards   # the editor is built with createElement on demand
    assert 'registerAction("edit-field"' in cards and 'data-action="edit-field"' in cards
    assert not re.search(r"\son[a-z]+\s*=", cards)


def test_library_renders_incrementally_with_a_button_fallback():
    lib = (LIBRARY / "panel.js").read_text(encoding="utf-8")
    assert "IntersectionObserver" in lib and 'registerAction("show-more"' in lib and 'data-action="show-more"' in lib


def test_no_where_to_watch_chips_remain_in_the_ui():
    """The TMDB provider data is gone (SIMKL does not supply it): nothing renders or styles provider chips."""
    for path in (JS / "util.js", LIBRARY / "cards.js", STATIC / "dialogs" / "details" / "dialog.js", STATIC / "panels" / "decide" / "panel.js"):
        assert "providerChips" not in path.read_text(encoding="utf-8"), path.name
    web = STATIC.parent
    assert "result-providers" not in (web / "templates" / "index.html").read_text(encoding="utf-8")
    assert "result-providers" not in (STATIC / "panels" / "decide" / "panel.html").read_text(encoding="utf-8")
    for css in STATIC.rglob("*.css"):
        assert "provider-chip" not in css.read_text(encoding="utf-8"), css.name
    assert "providers" not in CARD_FIELDS
