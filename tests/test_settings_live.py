"""Live apply: each setting that has an effect on a running part changes that part
with no restart.

The settings manager is not used here, so `FakeManager` does what the manager does after a commit: the
in-place copy of every `live` key into the running config (a `live: False` key is never copied), then
`hub.publish(Change)`. The components are the real ones (`register` is called with a Context that has the hub and the
manager), and every observer reads the effect from the running part: the clock, the log level, the Grafana links, the
sync library, the notifier, the dispatcher, the optimizer's plan, the History answer, `/mcp` and `/api/themes`."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import activity, clock, grafana, notify, webhooks_component
from penguwatch.components import Context
from penguwatch.config import HouseholdMember, WebhookConfig
from penguwatch.drivers.simkl.client import SimklClient
from penguwatch.main import app
from penguwatch.models import Show, ShowStatus
from penguwatch.notify import Event, Notifier, notify_worker
from penguwatch.settings import appliers, registry
from penguwatch.settings.hub import Change, SettingsHub
from penguwatch.sync import worker
from penguwatch import notify_component
from tests.mcp_helpers import no_network  # noqa: F401  (fixture)
from tests.simkl_helpers import simkl_engine

NEW_URL = "https://ntfy.example.com/family-topic"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class FakeManager:
    """What the settings manager does after a commit, and `secret_value` (never logged)."""

    def __init__(self, config, hub: SettingsHub):
        self.config, self.hub, self.secrets = config, hub, {}

    def secret_value(self, slot):
        return self.secrets.get(slot) or None

    def _owner(self, key):
        section = key.split(".")[0]
        return getattr(self.config, section, None) or self.config.driver_options(section)

    async def put(self, values=None, items=None, slots=None):
        """values {key: value}; items: the webhooks list (dicts without a url); slots {slot: value, "" clears}."""
        keys, lists = set(), set()
        for key, value in (values or {}).items():
            if not registry.get(key).live:
                continue  # copy_into never copies a live: false key
            setattr(self._owner(key), key.split(".", 1)[1], value)
            keys.add(key)
        self.secrets.update(slots or {})
        if items is not None:
            self._items = items
            lists.add("webhooks")
        if hasattr(self, "_items"):
            self.config.webhooks[:] = [WebhookConfig(**i, url=self.secrets[f"webhook.{i['id']}.url"])
                                       for i in self._items if self.secrets.get(f"webhook.{i['id']}.url")]
        return await self.hub.publish(Change.of(keys=keys, lists=lists, slots=slots or ()))


@dataclass
class Rig:
    config: Any
    hub: SettingsHub
    manager: FakeManager
    container: Any
    engine: Any
    ctx: Context
    notifier: Notifier
    dispatcher: Any
    optimizer: Any
    repo: Any


@pytest.fixture
async def rig(container, monkeypatch):
    for name in ("TZ", "PENGUWATCH_NOTIFY_URL"):
        monkeypatch.delenv(name, raising=False)
    config, repo = container.config, container.repository
    root = logging.getLogger()
    saved = (root.level, activity.tracker.idle_after_seconds, activity.tracker.on_resume)
    root.setLevel(logging.INFO)
    clock.configure(None)
    grafana.configure("")
    hub = SettingsHub()
    appliers.register_core(hub, config, repo)
    manager = FakeManager(config, hub)
    engine = simkl_engine(SimklClient(client_id="x", access_token="y"), repo, config.driver_options("simkl"))
    ctx = Context(app=app, config=config, repository=repo, settings=hub, settings_manager=manager, sync=engine,
                  optimizer=container.optimizer, container=container)
    await worker.register(ctx)
    await notify_component.register(ctx)
    hooks = await webhooks_component.register(ctx)
    yield Rig(config, hub, manager, container, engine, ctx, ctx.notifier, ctx.webhooks, container.optimizer, repo)
    for task in list(ctx.tasks):
        task.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    await hooks.stop()
    root.setLevel(saved[0])
    activity.tracker.idle_after_seconds, activity.tracker.on_resume = saved[1], saved[2]
    grafana._links.clear()
    clock.configure(None)


# --- the observers: each reads the effect from the running part ---------------------------------------------------

@dataclass
class Entry:
    new: Any
    observe: Callable[[Rig], Awaitable[Any]]
    expect: Any = None  # what the observer shows after; defaults to `new`
    prepare: Any = None  # async (rig): sets the scene before the first observation


def sync_attr(name):
    async def observe(rig):
        return getattr(rig.engine.library, name)
    return observe


async def _plan(rig):
    return await rig.optimizer.calculate_plan()


async def _digest_round(rig):
    """One round of the real notify_worker at 10:00 UTC with an enabled notifier and a stub build_events, which
    records what the worker asked for. Returns (sent, renewal_warning_days asked for)."""
    sent = []

    async def send(event):
        sent.append(event.key)
    rig.notifier.send = send
    asked = []

    async def build(repo, optimizer, today=None, renewal_warning_days=3):
        asked.append(renewal_warning_days)
        return [Event(key=f"k{len(asked)}", title="t", message="m")]

    real_build, real_now = notify.build_events, clock.utc_now
    notify.build_events = build
    clock.utc_now = lambda: datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
    try:
        task = asyncio.ensure_future(notify_worker(rig.repo, rig.optimizer, rig.notifier, config=rig.config.notify,
                                                   poll_seconds=0.01))
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        notify.build_events, clock.utc_now = real_build, real_now
    return bool(sent), (asked[0] if asked else None)


async def _sent(rig):
    return (await _digest_round(rig))[0]


async def _asked_days(rig):
    return (await _digest_round(rig))[1]


async def _mcp_status(rig):
    async with client() as ac:
        return (await ac.post("/mcp", content='{"jsonrpc":"2.0","id":1,"method":"ping"}', headers=MCP_HEADERS)).status_code


async def _theme_default(rig):
    async with client() as ac:
        return (await ac.get("/api/themes")).json()["default"]


async def _effects_default(rig):
    async with client() as ac:
        return (await ac.get("/api/themes")).json()["effects"]


async def _bulk_groups(rig):
    async with client() as ac:
        return (await ac.get("/api/history", params={"days": 7})).json()["bulk_groups"]


async def _seed_bulk(repo):
    """Three episodes that share one exact stamp, yesterday: a bulk group at the default threshold of 3."""
    stamp = (clock.utc_now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sid = await repo.upsert_show(Show(ids={"simkl": 7001}, title="Bulk", runtime_minutes=30, status=ShowStatus.WATCHING))
    db = await repo.get_connection()
    await db.executemany("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 1, ?, 1, ?)",
                         [(sid, n, stamp) for n in (1, 2, 3)])
    await db.commit()


def config_value(section, name):
    async def observe(rig):
        owner = getattr(rig.config, section, None) or rig.config.driver_options(section)
        return getattr(owner, name)
    return observe


ENTRIES: Dict[str, Entry] = {
    # household: the in-place copy (the names are read per request; the PUT answer says `reload`) and the repository's
    # member ids (which of a title's stored people count), from the core applier
    "household.members": Entry([HouseholdMember(id="person1", name="Ana"), HouseholdMember(id="person3", name="Ben")],
                               lambda r: _members(r), expect=(["Ana", "Ben"], ("person1", "person3"))),
    # sync intervals: the library's attributes, which the worker reads each loop
    "simkl.auto_sync_interval_minutes": Entry(10, sync_attr("pull_interval_minutes")),
    "simkl.idle_after_hours": Entry(24, sync_attr("idle_after_hours")),
    "simkl.idle_sync_interval_minutes": Entry(240, sync_attr("idle_pull_interval_minutes")),
    # optimizer: the plan the running optimizer calculates
    "optimizer.max_active_services": Entry(5, lambda r: _attr(_plan(r), "max_active_services")),
    "optimizer.household_priority_boost": Entry(7.5, lambda r: _boost(r, "household_priority_boost")),
    "optimizer.ready_to_binge_boost": Entry(6.5, lambda r: _boost(r, "ready_to_binge_boost")),
    "optimizer.value_window_days": Entry(180, lambda r: _attr(_plan(r), "value_window_days")),
    "optimizer.unused_hours": Entry(4.0, lambda r: _unused(r)),
    # digest
    "notify.kind": Entry("webhook", lambda r: _value(r.notifier.kind)),
    # the worker sends at 10:00 with hour 9 (True), not with hour 23 (False)
    "notify.hour": Entry(23, _sent, expect=False, prepare=lambda r: r.manager.put(slots={"notify.url": NEW_URL})),
    "notify.renewal_warning_days": Entry(9, _asked_days, prepare=lambda r: r.manager.put(slots={"notify.url": NEW_URL})),
    # display / integrations
    "display.theme": Entry("nord", _theme_default),
    "display.effects": Entry(False, _effects_default),
    "server.timezone": Entry("Pacific/Auckland", lambda r: _value(str(clock.tz()))),
    "mcp.enabled": Entry(True, _mcp_status, expect=200),
    "display.grafana_url": Entry("https://grafana.example.com:3000", lambda r: _value(
        [link["url"] for link in grafana.links()][:1]), expect=["https://grafana.example.com:3000/d/penguwatch/penguwatch"]),
    # advanced
    "history.bulk_min": Entry(4, _bulk_groups, expect=0, prepare=lambda r: _seed_bulk(r.repo)),
    "server.log_level": Entry("debug", lambda r: _value(logging.getLogger().level), expect=logging.DEBUG),
    "tvmaze.schedule_max_titles": Entry(200, config_value("tvmaze", "schedule_max_titles")),
}
RESTART = {key for key, setting in registry.BY_KEY.items() if not setting.live}


async def _attr(awaitable, name):
    return getattr(await awaitable, name)


async def _value(value):
    return value


async def _boost(rig, name):
    return getattr(rig.optimizer.config, name)


async def _unused(rig):
    return rig.optimizer.unused_hours


async def _members(rig):
    return [m.name for m in rig.config.household.members], rig.repo.household_members


def test_every_registry_key_has_an_entry_or_is_a_restart_key():
    live = {key for key, setting in registry.BY_KEY.items() if setting.live}
    assert set(ENTRIES) == live, (sorted(live - set(ENTRIES)), sorted(set(ENTRIES) - live))
    assert RESTART == {"drivers.library", "drivers.catalog", "drivers.schedule", "drivers.discovery", "simkl.app_name",
                       "tvmaze.rate_limit_calls", "tvmaze.rate_window_seconds", "tvmaze.timeout_seconds"}


@pytest.mark.parametrize("key", sorted(ENTRIES))
async def test_a_live_setting_changes_the_running_part_without_a_restart(rig, key, no_network):
    entry = ENTRIES[key]
    expect = entry.new if entry.expect is None else entry.expect
    if entry.prepare:
        await entry.prepare(rig)
    assert await entry.observe(rig) != expect, "the observer must see the old behaviour first"
    assert await rig.manager.put(values={key: entry.new}) == []
    assert await entry.observe(rig) == expect


@pytest.mark.parametrize("key", sorted(RESTART))
async def test_a_restart_setting_has_no_applier_and_the_running_value_stays(rig, key):
    owner_before = registry.get(key).default()
    assert rig.hub.subscribers(Change.of(keys=[key])) == []
    await rig.manager.put(values={key: "changed"})
    assert str(config_value_now(rig.config, key)) != "changed" and config_value_now(rig.config, key) == owner_before


def config_value_now(config, key):
    section, name = key.split(".", 1)
    owner = getattr(config, section, None) or config.driver_options(section)
    return getattr(owner, name)


def test_the_appliers_of_the_contract_are_registered_by_their_components(rig):
    assert sorted(rig.hub.names) == sorted(["clock.zone", "logging.level", "grafana.links", "household.members",
                                            "sync.intervals", "notify.channel", "webhooks.hooks"])
    assert rig.hub.subscribers(Change.of(keys=["server.timezone"])) == ["clock.zone"]
    assert rig.hub.subscribers(Change.of(slots=["notify.url"])) == ["notify.channel"]
    assert rig.hub.subscribers(Change.of(lists=["webhooks"])) == ["webhooks.hooks"]
    assert rig.hub.subscribers(Change.of(slots=["webhook.home.secret"])) == ["webhooks.hooks"]
    # settings the optimizer, History, /mcp and the page read per use have no applier
    for key in ("optimizer.value_window_days", "optimizer.unused_hours", "history.bulk_min", "mcp.enabled", "display.theme",
                "display.effects"):
        assert rig.hub.subscribers(Change.of(keys=[key])) == [], key


# --- the parts that need more than a table row ---------------------------------------------------------------------

async def test_the_digest_address_turns_the_notifier_on_and_off_with_no_restart(rig):
    assert rig.notifier.enabled is False
    assert await rig.manager.put(slots={"notify.url": NEW_URL}) == []
    assert rig.notifier.enabled and rig.notifier.url == NEW_URL
    assert await rig.manager.put(slots={"notify.url": ""}) == []
    assert rig.notifier.enabled is False


async def test_the_notify_worker_always_runs_and_follows_the_address_and_the_hour(rig):
    """The worker is started whatever the address; it sends nothing while the notifier is off, sends once the address
    is set, and not before the (live) hour."""
    sent = []

    async def send(event):
        sent.append(event.key)
    rig.notifier.send = send
    real_build, real_now = notify.build_events, clock.utc_now

    async def build(repo, optimizer, today=None, renewal_warning_days=3):
        return [Event(key="k", title="t", message="m")]
    notify.build_events = build
    clock.utc_now = lambda: datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
    try:
        rig.config.notify.hour = 12
        task = asyncio.ensure_future(notify_worker(rig.repo, rig.optimizer, rig.notifier, config=rig.config.notify,
                                                   poll_seconds=30))
        await asyncio.sleep(0.05)
        assert sent == []                                      # off: nothing sent
        await rig.manager.put(slots={"notify.url": NEW_URL})   # wakes the worker: no 10-minute wait
        await asyncio.sleep(0.05)
        assert sent == []                                      # on, but 10:00 is before the hour 12
        rig.config.notify.hour = 9
        await rig.manager.put(values={"notify.hour": 9})
        await asyncio.sleep(0.05)
        assert sent == ["k"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        notify.build_events, clock.utc_now = real_build, real_now


async def test_the_renewal_warning_days_reach_the_digest_events(rig):
    from datetime import date
    renew = date.today() + timedelta(days=6)
    services = await rig.repo.get_services()
    apple = next(s for s in services if s.id == "apple_tv")
    apple.renewal_date = renew.isoformat()
    await rig.repo.upsert_service(apple)
    assert [e for e in await notify.build_events(rig.repo, rig.optimizer, renewal_warning_days=3)
            if e.key.startswith("renewal")] == []
    events = await notify.build_events(rig.repo, rig.optimizer, renewal_warning_days=7)
    assert [e.key.split(":")[0] for e in events] == ["renewal"]


async def test_the_optimizer_uses_the_unused_threshold_and_window_it_is_given(rig):
    """Hours are compared with the live threshold: the same service is 'unused' or not by the setting alone."""
    sid = await rig.repo.upsert_show(Show(ids={"simkl": 7002}, title="Some", service_id="apple_tv", runtime_minutes=60,
                                          status=ShowStatus.WATCHING))
    stamp = (clock.utc_now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = await rig.repo.get_connection()
    await db.execute("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 1, 1, 1, ?)",
                     (sid, stamp))
    await db.commit()

    async def verdict():
        return next(r for r in (await rig.optimizer.calculate_plan()).service_rankings if r.service_id == "apple_tv").verdict

    assert await verdict() != "unused"              # 1 hour is above 0.5
    await rig.manager.put(values={"optimizer.unused_hours": 3.0})
    assert await verdict() == "unused"              # and below 3
    await rig.manager.put(values={"optimizer.unused_hours": 0.5, "optimizer.value_window_days": 30})
    assert (await rig.optimizer.calculate_plan()).value_window_days == 30


async def test_the_history_cache_is_keyed_by_the_threshold(rig):
    """The cached answer made with 3 must not be served after the threshold is 4 (and comes back for 3)."""
    await _seed_bulk(rig.repo)
    repo = rig.repo
    assert (await repo.get_history(7, False, 3))["bulk_groups"] == 1
    assert (await repo.get_history(7, False, 4))["bulk_groups"] == 0
    assert (await repo.get_history(7, False, 3))["bulk_groups"] == 1
    assert (await repo.get_history(7))["bulk_groups"] == 1          # the default is still 3


async def test_the_zone_applier_moves_today_across_midnight(rig, monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: datetime(2026, 11, 6, 23, 30, tzinfo=timezone.utc))
    assert clock.today().isoformat() == "2026-11-06"
    await rig.manager.put(values={"server.timezone": "Pacific/Auckland"})
    assert clock.today().isoformat() == "2026-11-07"
    await rig.manager.put(values={"server.timezone": ""})
    assert clock.today().isoformat() == "2026-11-06"


# --- the sync worker -----------------------------------------------------------------------------------------------

class _Library:
    name, label, remote = "fake", "Fake", False
    pull_interval_minutes, idle_pull_interval_minutes, idle_after_hours = 600, 600, 12

    def is_connected(self):
        return True

    async def maintain(self):
        return None


class _Shim:
    """asyncio as the worker module sees it (tests/test_housekeeping.py): no 5 s pause, short waits."""
    TimeoutError, CancelledError = asyncio.TimeoutError, asyncio.CancelledError
    get_running_loop = staticmethod(asyncio.get_running_loop)

    async def sleep(self, seconds):
        await asyncio.sleep(0)

    async def wait_for(self, awaitable, timeout):
        return await asyncio.wait_for(awaitable, timeout=0.02)


async def test_the_worker_pulls_sooner_after_the_interval_is_shortened_and_never_restarted(monkeypatch):
    pulls = []

    async def sync():
        pulls.append(1)
        return {}

    async def nothing(*a, **k):
        return {}

    async def gate():
        return "open"

    class Repo:
        async def delete_states_before(self, *a):
            return 0
    engine = SimpleNamespace(library=_Library(), repository=Repo(), kick=asyncio.Event(), reconnect_gate=gate, sync=sync,
                             flush_outbox=nothing)
    tracker = SimpleNamespace(take_resumed=lambda: False, is_idle=lambda: False, idle_after_seconds=0)
    monkeypatch.setattr(worker, "asyncio", _Shim())
    task = asyncio.ensure_future(worker.sync_worker(engine, tracker=tracker))   # intervals from engine.library
    for _ in range(100):
        await asyncio.sleep(0.01)
        if pulls:
            break
    assert len(pulls) == 1
    await asyncio.sleep(0.15)
    assert len(pulls) == 1                                    # next pull is 600 minutes away
    options = SimpleNamespace(auto_sync_interval_minutes=0.001, idle_sync_interval_minutes=0.001, idle_after_hours=1)
    worker.apply_intervals(engine, options, tracker)          # what the sync.intervals applier does
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(pulls) >= 2:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(pulls) >= 2
    assert tracker.idle_after_seconds == 3600


async def test_the_intervals_applier_sets_the_library_the_tracker_and_kicks_the_worker(rig):
    rig.engine.kick.clear()
    await rig.manager.put(values={"simkl.auto_sync_interval_minutes": 7, "simkl.idle_after_hours": 5})
    assert rig.engine.library.pull_interval_minutes == 7 and rig.engine.library.idle_after_hours == 5
    assert activity.tracker.idle_after_seconds == 5 * 3600
    assert rig.engine.kick.is_set()


async def test_a_failing_applier_is_contained_named_and_its_message_is_not_logged(rig, monkeypatch, caplog):
    def boom(url):
        raise ValueError("the value is https://secret.example/token")
    monkeypatch.setattr(grafana, "configure", boom)
    failed = await rig.manager.put(values={"display.grafana_url": "https://grafana.example.com"})
    assert failed == ["grafana.links"]
    assert "secret.example" not in caplog.text and "grafana.links" in caplog.text
