"""A seeded container for the MCP tests: a small library (two shows in progress, a movie, a finished show, one episode
airing soon), the optimizer's services, and a marker string in every slot that must never reach a tool's output (the
access token, the SIMKL login, the notification and webhook addresses and secrets, a last error that quotes a login)."""

from datetime import timedelta

import socket

import pytest

from penguwatch import clock
from penguwatch.config import WebhookConfig
from penguwatch.drivers.simkl import auth as simkl_auth
from penguwatch.drivers.simkl.service import KEY_ACCESS, KEY_CLIENT_ID, KEY_REFRESH
from penguwatch.models import MediaType, Priority, Show, ShowStatus

TOKEN = "CANARY-access-token-0001"
SECRETS = {
    "access_token": TOKEN,
    "simkl.client_id": "CANARY-simkl-client-id-0002",
    "simkl.client_secret": "CANARY-simkl-client-secret-0003",
    "simkl.access_token": "CANARY-simkl-access-token-0004",
    "notify.url": "https://ntfy.example.com/CANARY-ntfy-url-0005",
    "webhook.url": "https://hooks.example.com/CANARY-webhook-url-0006",
    "webhook.secret": "CANARY-webhook-secret-0007",
    "simkl.refresh_token": "CANARY-simkl-refresh-token-0008",
    "db.access_token": "CANARY-db-access-token-0009",
    "db.client_id": "CANARY-db-client-id-0010",
    "auth_message": "CANARY-auth-message-0011",
    "webhook.file_secret": "CANARY-webhook-file-secret-0012",
}
MARKERS = tuple(SECRETS.values())

TOOL_CALLS = [
    ("up_next", {}), ("tonights_pick", {}), ("search_library", {"query": "e"}), ("title", {"name": "Severance"}),
    ("schedule", {"days_ahead": 30}), ("optimizer", {}), ("sync_status", {}),
    ("history", {"days": 7}),
]


@pytest.fixture
def no_network(monkeypatch):
    """Fails the test when anything tries to leave the machine: names are not resolved and only loopback is connected
    to (the SDK end-to-end tests talk to a server on 127.0.0.1)."""
    attempts = []
    resolve, connect = socket.getaddrinfo, socket.socket.connect

    def loopback(host) -> bool:
        return host in (None, "", "localhost", "127.0.0.1", "::1") or str(host).startswith("127.")

    def getaddrinfo(host, *args, **kw):
        if not loopback(host):
            attempts.append(f"resolve {host}")
            raise OSError("the tests are not allowed on the network")
        return resolve(host, *args, **kw)

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not loopback(address[0]):
            attempts.append(f"connect {address[0]}")
            raise OSError("the tests are not allowed on the network")
        return connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield attempts
    assert attempts == [], f"the test reached for the network: {attempts}"


async def seed(container, monkeypatch, tmp_path):
    """Fills the container's library and plants the secret markers everywhere the app keeps one."""
    repo = container.repository
    today = clock.today()

    async def add(title, kind, status, who, prio, service, imdb, episodes, watched, **extra):
        show = Show(title=title, year=2022, media_type=kind, status=status, household=who, priority=prio,
                    service_id=service, imdb_rating=imdb, genres=["Drama"], overview=f"{title} is invented.", **extra)
        show_id = await repo.upsert_show(show)
        if episodes:
            await repo.upsert_episode_list(show_id, [
                {"season": 1, "number": n + 1, "title": f"Chapter {n + 1}",
                 "air_date": (today + timedelta(days=days)).isoformat()} for n, days in enumerate(episodes)])
            await repo.set_episodes_watched(show_id, [(1, n + 1) for n in range(watched)], True,
                                            watched_at=(today - timedelta(days=2)).isoformat() + "T20:00:00+00:00")
            await repo.update_show_fields(show_id, {"total_episodes": len(episodes),
                                                    "episodes_synced_at": today.isoformat() + "T00:00:00+00:00",
                                                    "last_watched_at": (today - timedelta(days=2)).isoformat() + "T20:00:00+00:00"})
            await repo.recompute_progress_from_episodes(show_id)
        return show_id

    ids = {
        "Severance": await add("Severance", MediaType.SHOW, ShowStatus.WATCHING, ["person1", "person2"], Priority.HIGH,
                               "apple_tv", 8.7, [-30, -23, -16, -9, 3], 2),
        "Andor": await add("Andor", MediaType.SHOW, ShowStatus.WATCHING, ["person1"], Priority.MEDIUM,
                           "netflix", 8.4, [-60, -53, -46], 1),
        "Heat": await add("Heat", MediaType.MOVIE, ShowStatus.PLANTOWATCH, ["person2"], Priority.LOW,
                          None, 8.3, [], 0, total_episodes=1),
        "Old Show": await add("Old Show", MediaType.SHOW, ShowStatus.COMPLETED, ["person1"], Priority.LOW,
                              "netflix", 7.0, [-300, -293], 2),
    }
    await repo.update_show_fields(ids["Severance"], {"user_rating": 9.0})

    # the secrets, each where the app keeps it
    monkeypatch.setattr(container.security, "access_token", SECRETS["access_token"])
    simkl = container.drivers.by_name["simkl"]
    for target in (simkl.client, ):
        target.client_id = SECRETS["simkl.client_id"]
        target.client_secret = SECRETS["simkl.client_secret"]
        target.access_token = SECRETS["simkl.access_token"]
    options = container.config.driver_options("simkl")
    options.client_id, options.access_token = SECRETS["simkl.client_id"], SECRETS["simkl.access_token"]
    monkeypatch.setenv("PENGUWATCH_NOTIFY_URL", SECRETS["notify.url"])
    monkeypatch.setenv("PW_TEST_HOOK_SECRET", SECRETS["webhook.secret"])
    secret_file = tmp_path / "hook-secret"
    secret_file.write_text(SECRETS["webhook.file_secret"])
    container.config.webhooks = [
        WebhookConfig(url=SECRETS["webhook.url"], secret_env="PW_TEST_HOOK_SECRET", name="hook"),
        WebhookConfig(url="https://hooks.example.com/other", secret_file=str(secret_file), name="other")]
    simkl.client.refresh_token = SECRETS["simkl.refresh_token"]
    # what the app has stored in its database: the login, and the text of a refused login
    await repo.set_states({KEY_ACCESS: SECRETS["db.access_token"], KEY_CLIENT_ID: SECRETS["db.client_id"],
                           KEY_REFRESH: SECRETS["simkl.refresh_token"], simkl_auth.KEY_STATE: simkl_auth.NEEDS_RECONNECT,
                           simkl_auth.KEY_MSG: f"Login refused for {SECRETS['auth_message']}"})
    await repo.set_state(container.sync.state_key("last_sync"), "2026-11-05T20:30:00+00:00")
    await repo.set_state(container.sync.state_key("last_error"), f"HTTP 401 from https://api.simkl.com/sync?client_id={SECRETS['simkl.client_id']}"
                         f" (token {SECRETS['simkl.access_token']}, webhook {SECRETS['webhook.url']})")
    return ids
