"""Household clock: configured IANA zone drives today()/now(); storage stays UTC."""

from datetime import datetime, timezone

import pytest

from penguwatch import clock
from penguwatch.config import load_config


@pytest.fixture(autouse=True)
def _reset_zone(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    clock.configure(None)
    yield
    clock.configure(None)


def _freeze(monkeypatch, *args):
    instant = datetime(*args, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "utc_now", lambda: instant)


def test_default_zone_is_utc(monkeypatch):
    _freeze(monkeypatch, 2026, 10, 10, 23, 30)
    assert clock.tz() is timezone.utc
    assert clock.today().isoformat() == "2026-10-10"
    assert clock.now().utcoffset().total_seconds() == 0


def test_configured_zone_rolls_over_at_local_midnight(monkeypatch):
    clock.configure("America/New_York")  # UTC-4 in October
    _freeze(monkeypatch, 2026, 10, 10, 3, 59)
    assert clock.today().isoformat() == "2026-10-09"
    _freeze(monkeypatch, 2026, 10, 10, 4, 0)
    assert clock.today().isoformat() == "2026-10-10"
    assert clock.now().hour == 0


def test_invalid_zone_falls_back_to_utc(monkeypatch):
    clock.configure("Not/AZone")
    _freeze(monkeypatch, 2026, 10, 10, 3, 59)
    assert clock.tz() is timezone.utc
    assert clock.today().isoformat() == "2026-10-10"


def test_tz_env_is_default(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    clock.configure("")
    _freeze(monkeypatch, 2026, 10, 10, 20, 0)
    assert clock.today().isoformat() == "2026-10-11"


def test_explicit_zone_beats_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    clock.configure("UTC")
    assert clock.tz() is timezone.utc


def test_utc_now_is_aware_utc():
    assert clock.utc_now().utcoffset().total_seconds() == 0


def test_env_override_read_by_load_config(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUWATCH_TZ", "Europe/Berlin")
    assert load_config(str(tmp_path / "none.yaml")).server.timezone == "Europe/Berlin"


def test_config_default_is_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("PENGUWATCH_TZ", raising=False)
    assert load_config(str(tmp_path / "none.yaml")).server.timezone == ""
