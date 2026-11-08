"""Notification events, de-duplication and delivery formats."""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

from penguwatch.config import OptimizerConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show, ServiceConfig, ShowStatus
from penguwatch import notify
from penguwatch.notify import Event, Notifier, build_events, deliver_new
from penguwatch.optimizer.engine import SubscriptionOptimizer


@pytest.fixture(autouse=True)
def unguarded_transport(monkeypatch):
    """respx mocks by URL; the guard (tests/test_settings_webhooks.py) rewrites the URL to the resolved address."""
    monkeypatch.setattr(notify, "GuardedTransport", lambda: httpx.AsyncHTTPTransport())


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "n.db"))
    renew_in_2 = date.today() + timedelta(days=2)
    await repo.initialize([
        ServiceConfig(id="idle", name="Idle+", monthly_cost=8.0, is_active=True, renewal_date=renew_in_2.isoformat()),
        ServiceConfig(id="live", name="Live+", monthly_cost=10.0, is_active=True),
    ])
    yield repo, SubscriptionOptimizer(repository=repo, config=OptimizerConfig())
    await repo.close()


@pytest.mark.asyncio
async def test_renewal_and_airing_today_events(env):
    repo, opt = env
    await repo.upsert_show(Show(title="Gathering Dust", service_id="idle", status=ShowStatus.PLANTOWATCH,
                                total_episodes=4, aired_episodes=4, watched_episodes=0))
    await repo.upsert_show(Show(title="Weekly Thing", service_id="live", status=ShowStatus.WATCHING,
                                total_episodes=10, aired_episodes=5, watched_episodes=5,
                                next_episode_air_date=date.today().isoformat(), next_episode_season=2, next_episode_number=6,
                                next_episode_title="The Reveal"))
    events = {e.key.split(":")[0]: e for e in await build_events(repo, opt)}
    assert "Idle+ renews in 2 day(s)" == events["renewal"].title and events["renewal"].priority == 4
    assert events["air"].title == "New today: Weekly Thing S02E06"
    assert "The Reveal" in events["air"].message


@pytest.mark.asyncio
@respx.mock
async def test_each_event_is_delivered_once(env):
    repo, _ = env
    route = respx.post("https://ntfy.example/topic").mock(return_value=httpx.Response(200))
    notifier = Notifier("https://ntfy.example/topic", "ntfy")
    events = [Event(key="a", title="One", message="m1", priority=4, tags=["x", "y"]), Event(key="b", title="Two", message="m2")]
    assert await deliver_new(repo, notifier, events) == 2
    assert await deliver_new(repo, notifier, events) == 0  # already sent
    assert route.call_count == 2
    req = route.calls[0].request
    assert req.headers["Title"] == "One" and req.headers["Priority"] == "4" and req.headers["Tags"] == "x,y"
    assert req.content == b"m1"


@pytest.mark.asyncio
@respx.mock
async def test_failed_delivery_is_retried_next_time(env):
    repo, _ = env
    respx.post("https://hook.example/h").mock(return_value=httpx.Response(500))
    notifier = Notifier("https://hook.example/h", "webhook")
    ev = [Event(key="k", title="T", message="M")]
    assert await deliver_new(repo, notifier, ev) == 0
    respx.post("https://hook.example/h").mock(return_value=httpx.Response(200, json={}))
    assert await deliver_new(repo, notifier, ev) == 1
    assert respx.calls.last.request.headers["content-type"] == "application/json"


def test_not_enabled_without_http_url():
    assert not Notifier("").enabled and not Notifier("ntfy.sh/topic").enabled
    assert Notifier("https://ntfy.sh/topic").enabled
