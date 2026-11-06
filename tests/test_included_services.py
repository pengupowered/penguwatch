"""A service that costs nothing extra (Prime Video with Amazon Prime, Apple TV+ via another subscription)
must stay at $0 across restarts: the startup seeding may never treat 0 as "unset"."""

from pathlib import Path

import pytest

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig

PANELS = Path(__file__).resolve().parent.parent / "src" / "penguwatch" / "web" / "static" / "panels"

DEFAULTS = [
    ServiceConfig(id="prime_video", name="Prime Video", monthly_cost=8.99, aliases=["Amazon"]),
    ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99),
]


@pytest.mark.asyncio
async def test_a_zero_price_survives_a_restart(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "included.db"))
    await repo.initialize(DEFAULTS)
    await repo.upsert_service(
        ServiceConfig(id="prime_video", name="Prime Video", monthly_cost=0.0, is_active=True,
                      notes="Included with Amazon Prime", aliases=["Amazon"])
    )
    await repo.close()

    again = DatabaseRepository(str(tmp_path / "included.db"))
    await again.initialize(DEFAULTS)  # what every container start does
    prime = await again.get_service("prime_video")
    other = await again.get_service("apple_tv")
    await again.close()

    assert prime.monthly_cost == 0.0
    assert prime.is_active is True
    assert prime.notes == "Included with Amazon Prime"
    assert other.monthly_cost == 9.99  # untouched service keeps its configured price


@pytest.mark.asyncio
async def test_new_built_in_services_still_get_their_configured_price(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "new.db"))
    await repo.initialize([DEFAULTS[0]])
    await repo.close()

    again = DatabaseRepository(str(tmp_path / "new.db"))
    await again.initialize(DEFAULTS)  # apple_tv appears in a later release
    assert (await again.get_service("apple_tv")).monthly_cost == 9.99
    assert (await again.get_service("prime_video")).monthly_cost == 8.99
    await again.close()


def test_optimizer_card_says_included_and_offers_no_cancel_for_free_services():
    js = (PANELS / "optimizer" / "panel.js").read_text(encoding="utf-8")
    assert "Included (no extra cost)" in js
    assert 'if (rec.is_free) return "";' in js


def test_services_tab_explains_a_zero_price():
    js = (PANELS / "services" / "panel.js").read_text(encoding="utf-8")
    assert "Included, no extra cost" in js
