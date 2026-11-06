"""Bundle coverage: pure functions and their agreement with the SQL filters and dashboard stats."""

import pytest

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show, ShowStatus, ServiceConfig
from penguwatch.optimizer import bundles


def svc(id, cost=0.0, active=False, includes=None):
    return ServiceConfig(id=id, name=id.title(), monthly_cost=cost, is_active=active, includes=includes or [])


def test_covered_member_is_effectively_active_and_free():
    hulu = svc("hulu", 9.99)
    combo = svc("combo", 21.99, True, ["hulu", "disney_plus"])
    cov = bundles.coverage([hulu, combo])
    assert cov == {"hulu": "combo", "disney_plus": "combo"}
    assert bundles.is_effectively_active(hulu, cov)
    assert not bundles.is_effectively_active(svc("netflix"), cov)
    assert bundles.monthly_spend([hulu, combo]) == 21.99
    assert bundles.paying_count([hulu, combo]) == 1


def test_inactive_bundle_covers_nothing():
    hulu = svc("hulu", 9.99, True)
    combo = svc("combo", 21.99, False, ["hulu"])
    assert bundles.coverage([hulu, combo]) == {}
    assert bundles.monthly_spend([hulu, combo]) == 9.99
    assert bundles.paying_count([hulu, combo]) == 1


def test_overlapping_bundles_cheapest_wins_ties_by_id():
    a = svc("a_bundle", 19.99, True, ["hulu"])
    b = svc("b_bundle", 14.99, True, ["hulu", "espn"])
    assert bundles.coverage([a, b]) == {"hulu": "b_bundle", "espn": "b_bundle"}
    c = svc("c_bundle", 14.99, True, ["hulu"])
    assert bundles.coverage([c, b])["hulu"] == "b_bundle"


def test_bundles_never_cover_bundles():
    inner = svc("inner", 5.0, True, ["hulu"])
    outer = svc("outer", 30.0, True, ["inner", "disney_plus"])
    cov = bundles.coverage([inner, outer])
    assert "inner" not in cov
    assert cov["disney_plus"] == "outer"
    assert bundles.paying_count([inner, outer]) == 2
    assert bundles.monthly_spend([inner, outer]) == 35.0


def test_spend_does_not_double_count_active_members():
    hulu = svc("hulu", 9.99, True)
    disney = svc("disney_plus", 13.99, True)
    netflix = svc("netflix", 15.49, True)
    combo = svc("combo", 21.99, True, ["hulu", "disney_plus"])
    assert bundles.monthly_spend([hulu, disney, combo]) == 21.99
    assert bundles.paying_count([hulu, disney, combo]) == 1
    assert bundles.monthly_spend([hulu, disney, combo, netflix]) == 37.48
    assert bundles.paying_count([hulu, disney, combo, netflix]) == 2


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "bundles_int.db"))
    await r.initialize([
        svc("hulu", 9.99),
        svc("disney_plus", 13.99),
        svc("netflix", 15.49),
        svc("combo", 21.99, True, ["hulu", "disney_plus"]),
    ])
    yield r
    await r.close()


async def test_sql_filters_and_stats_agree_with_pure_functions(repo):
    for title, sid in [("Hulu Show", "hulu"), ("Disney Show", "disney_plus"), ("Netflix Show", "netflix")]:
        await repo.upsert_show(Show(title=title, service_id=sid, status=ShowStatus.WATCHING))

    services = await repo.get_services()
    cov = bundles.coverage(services)
    expected = sorted(s.id for s in services if bundles.is_effectively_active(s, cov))
    assert expected == ["combo", "disney_plus", "hulu"]

    shows = await repo.get_shows(only_active_services=True)
    assert sorted(s.title for s in shows) == ["Disney Show", "Hulu Show"]

    up_next = await repo.get_up_next(only_active_services=True)
    assert sorted(e["title"] for e in up_next) == ["Disney Show", "Hulu Show"]
    assert all(e["service_active"] is True for e in up_next)
    everything = await repo.get_up_next()
    flags = {e["title"]: e["service_active"] for e in everything}
    assert flags == {"Hulu Show": True, "Disney Show": True, "Netflix Show": False}

    stats = await repo.get_dashboard_stats()
    assert stats["current_monthly_spend"] == bundles.monthly_spend(services) == 21.99
    assert stats["active_services_count"] == bundles.paying_count(services) == 1
    assert stats["all_services_spend"] == round(9.99 + 13.99 + 15.49 + 21.99, 2)


async def test_inactive_bundle_leaves_filters_unchanged(repo):
    await repo.upsert_show(Show(title="Hulu Show", service_id="hulu", status=ShowStatus.WATCHING))
    combo = await repo.get_service("combo")
    combo.is_active = False
    await repo.upsert_service(combo)
    assert await repo.get_shows(only_active_services=True) == []
    assert await repo.get_up_next(only_active_services=True) == []
