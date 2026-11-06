"""iCal feed: renewal and cancel-by events for paid services (api/ical.py), episode events untouched."""

from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import clock
from penguwatch.api.ical import billing_events, fold
from penguwatch.main import app
from penguwatch.models import OptimizerRecommendation, ServiceConfig, Show, ShowStatus

TODAY = date(2026, 10, 12)


def _rec(sid="netflix", name="Netflix", cost=19.99, renew_in=10, **kw):
    base = dict(
        service_id=sid, service_name=name, is_active=True, monthly_cost=cost, binge_score=1.0, hours_backlog=0.0,
        episodes_backlog=0, shows_count=0, ready_to_binge_count=0, both_want_count=0, recommendation_reason="",
        action="keep_active", next_renewal=(TODAY + timedelta(days=renew_in)).isoformat() if renew_in is not None else None,
        renewal_in_days=renew_in,
    )
    base.update(kw)
    return OptimizerRecommendation(**base)


def _events(lines):
    text = "\r\n".join(lines).replace("\r\n ", "")  # unfold
    return [e.split("\r\n") for e in text.split("BEGIN:VEVENT\r\n")[1:]]


def _field(event, name):
    return next(line.split(":", 1)[1] for line in event if line.startswith(name))


def test_renewal_event_is_an_all_day_event_with_a_stable_uid():
    lines = billing_events([_rec()], "20261012T000000Z", TODAY)
    (event,) = _events(lines)
    assert _field(event, "UID") == "wt-renew-netflix-20261022@penguwatch"
    assert "DTSTART;VALUE=DATE:20261022" in event and "DTEND;VALUE=DATE:20261023" in event
    assert _field(event, "SUMMARY") == "Renews: Netflix ($19.99)"
    assert billing_events([_rec()], "20261013T010101Z", TODAY)[1] == lines[1]  # same UID on the next fetch


def test_yearly_renewal_shows_the_yearly_price():
    (event,) = _events(billing_events([_rec(cost=10.0, billing_period="yearly", renew_in=100)], "s", TODAY))
    assert _field(event, "SUMMARY") == "Renews: Netflix ($120.00/yr)"


def test_unpaid_inactive_covered_or_dateless_services_get_no_event():
    recs = [
        _rec("free", "Free", cost=0.0), _rec("off", "Off", is_active=False), _rec("cov", "Cov", covered_by="bundle"),
        _rec("nodate", "NoDate", renew_in=None),
    ]
    assert billing_events(recs, "s", TODAY) == []


@pytest.mark.parametrize("status,expected", [("consider_cancel", True), ("decide_soon", True), ("in_use", False),
                                              ("watching", False), ("check", False), (None, False)])
def test_cancel_by_only_when_the_optimizer_says_cancel(status, expected):
    events = _events(billing_events([_rec(status=status, status_text="No viewing.")], "s", TODAY))
    summaries = [_field(e, "SUMMARY") for e in events]
    assert ("Cancel by: Netflix" in summaries) is expected
    if expected:
        cancel = next(e for e in events if _field(e, "SUMMARY") == "Cancel by: Netflix")
        assert "DTSTART;VALUE=DATE:20261021" in cancel  # the day before the renewal
        assert _field(cancel, "UID") == "wt-cancel-netflix-20261021@penguwatch"


def test_no_cancel_reminder_in_the_past():
    events = _events(billing_events([_rec(status="consider_cancel", renew_in=0)], "s", TODAY))
    assert [_field(e, "SUMMARY") for e in events] == ["Renews: Netflix ($19.99)"]


def test_special_characters_are_escaped_and_long_lines_folded():
    name = "Acorn TV; Drama, Mystery " + "é" * 60
    lines = billing_events([_rec(name=name)], "s", TODAY)
    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    (event,) = _events(lines)
    assert _field(event, "SUMMARY").startswith("Renews: Acorn TV\\; Drama\\, Mystery é")
    assert "".join(fold("x" * 200)).replace(" ", "") == "x" * 200


async def test_feed_has_episode_and_billing_events(container):
    repo = container.repository
    today = clock.today()
    in_ten = today + timedelta(days=10)
    await repo.upsert_service(ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, is_active=True,
                                            renewal_day=in_ten.day))
    show_id = await repo.upsert_show(Show(title="Slow Horses", service_id="apple_tv", status=ShowStatus.PLANTOWATCH,
                                          total_episodes=6, aired_episodes=6, watched_episodes=0))
    await repo.upsert_upcoming_episodes([{
        "show_id": show_id, "season": 5, "number": 1, "title": "Return",
        "air_date": (today + timedelta(days=2)).isoformat(),
    }])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/calendar.ics")
    assert res.status_code == 200
    text = res.text
    assert "SUMMARY:Slow Horses S05E01 – Return" in text
    assert "SUMMARY:Renews: Apple TV+ ($9.99)" in text
    assert f"UID:wt-renew-apple_tv-{in_ten.strftime('%Y%m%d')}@penguwatch" in text
    assert "Netflix" not in text  # inactive
    # unused paid service with its titles assigned: the optimizer says cancel, so a reminder the day before
    assert "Cancel by: Apple TV+" in text
    cancel_day = (in_ten - timedelta(days=1)).strftime("%Y%m%d")
    assert f"UID:wt-cancel-apple_tv-{cancel_day}@penguwatch" in text
    assert text.startswith("BEGIN:VCALENDAR") and text.rstrip().endswith("END:VCALENDAR")
