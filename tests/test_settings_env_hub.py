"""The environment rule (penguwatch.settings.env: empty = unset, one table of variables) and the settings hub
(penguwatch.settings.hub: subscriptions, order, containment)."""

import logging

import pytest

from penguwatch.settings import env as settings_env
from penguwatch.settings.document import SettingsDocument
from penguwatch.settings.hub import Change, SettingsHub


def test_an_empty_or_blank_variable_is_unset():
    environ = {"A": "", "B": "   ", "C": " value "}
    assert settings_env.env_value("A", environ) is None
    assert settings_env.env_value("B", environ) is None
    assert settings_env.env_value("C", environ) == "value"
    assert settings_env.env_value("MISSING", environ) is None


def test_overrides_are_read_through_the_table():
    environ = {"PENGUWATCH_TZ": "Europe/Berlin", "PENGUWATCH_LOG_LEVEL": "", "PENGUWATCH_NOTIFY_URL": "https://x/"}
    assert settings_env.setting_override("server.timezone", environ) == "Europe/Berlin"
    assert settings_env.setting_override("server.log_level", environ) is None
    assert settings_env.setting_override("optimizer.max_active_services", environ) is None
    assert settings_env.setting_overrides(environ) == {"server.timezone": "Europe/Berlin"}
    assert settings_env.secret_override("notify.url", environ) == "https://x/"
    assert settings_env.secret_override("webhook.home.url", environ) is None


def test_the_document_round_trips_and_leaves_default_lists_out():
    doc = SettingsDocument(values={"notify.hour": 7}, lists={"webhooks": None})
    assert doc.to_json() == {"values": {"notify.hour": 7}, "lists": {}}
    assert SettingsDocument.from_json(doc.to_json()).value("notify.hour") == 7
    assert SettingsDocument.from_json(None).list_items("webhooks") is None


@pytest.mark.asyncio
async def test_appliers_run_once_in_order_for_what_they_subscribe_to():
    hub, calls = SettingsHub(), []
    hub.on(keys=["server.timezone"], name="clock.zone")(lambda change: calls.append("clock"))
    hub.on(prefixes=["simkl."], name="sync.intervals")(lambda change: calls.append("sync"))

    async def hooks(change):
        calls.append("hooks")
    hub.on(lists=["webhooks"], slots=["webhook."], name="webhooks.hooks")(hooks)
    hub.on(slots=["notify.url"], keys=["notify.kind", "notify.hour"], name="notify.channel")(
        lambda change: calls.append("notify"))

    change = Change.of(keys=["simkl.idle_after_hours", "server.timezone"], slots=["webhook.home.url"])
    assert await hub.publish(change) == []
    assert calls == ["clock", "sync", "hooks"]
    assert hub.subscribers(Change.of(slots=["notify.url"])) == ["notify.channel"]
    assert hub.touched_by("webhooks.hooks", change) == ["webhook.home.url"]
    assert hub.names == ["clock.zone", "sync.intervals", "webhooks.hooks", "notify.channel"]
    assert Change.of().empty and not change.empty


@pytest.mark.asyncio
async def test_a_failing_applier_is_contained_named_and_counted_without_its_message(caplog):
    failures = []
    hub, calls = SettingsHub(on_failure=failures.append), []

    def broken(change):
        raise ValueError("https://secret.example/token-in-the-message")
    hub.on(keys=["server.log_level"], name="logging.level")(broken)
    hub.on(keys=["server.log_level"], name="after")(lambda change: calls.append("after"))
    with caplog.at_level(logging.ERROR, logger="penguwatch.settings"):
        failed = await hub.publish(Change.of(keys=["server.log_level"], new=SettingsDocument()))
    assert failed == ["logging.level"] and failures == ["logging.level"] and calls == ["after"]
    assert "ValueError" in caplog.text and "server.log_level" in caplog.text
    assert "token-in-the-message" not in caplog.text


def test_a_subscription_needs_a_name_once_and_something_to_hear():
    hub = SettingsHub()
    with pytest.raises(ValueError, match="nothing"):
        hub.on(name="empty")
    hub.on(keys=["notify.hour"], name="notify.hour")(lambda change: None)
    with pytest.raises(ValueError, match="twice"):
        hub.on(keys=["notify.kind"], name="notify.hour")
