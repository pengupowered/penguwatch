"""Service matching (penguwatch.servicematch): only the user's services, title rules first, whole-word aliases."""

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app
from penguwatch.models import ServiceConfig
from penguwatch.servicematch import match_service


def svc(sid, name, aliases=(), titles=()):
    return ServiceConfig(id=sid, name=name, aliases=list(aliases), titles=list(titles))


SERVICES = [
    svc("max", "Max", ["HBO", "HBO Max", "TBS"], titles=["Friends"]),
    svc("netflix", "Netflix", ["The CW"], titles=["Seinfeld"]),
    svc("hulu", "Hulu", ["FX", "FXX", "ABC"]),
    svc("crunchyroll", "Crunchyroll", ["TBS (JP)", "Tokyo MX"]),
    svc("britbox", "BritBox", ["BBC", "BBC Two"]),
    svc("amc_plus", "AMC+", ["AMC", "BBC America"]),
    svc("pbs", "PBS Passport", ["Smithsonian Channel"]),
    svc("uk5", "Five", ["5"]),
]


@pytest.mark.parametrize("network, title, expected", [
    ("HBO", None, "max"),                       # exact alias
    ("netflix", None, "netflix"),               # the service's own name, case ignored
    ("  Tokyo   MX ", None, "crunchyroll"),     # spaces folded
    ("TBS (JP)", None, "crunchyroll"),          # exact beats "TBS" contained in it
    ("BBC America", None, "amc_plus"),          # exact beats "BBC" contained in it
    ("BBC Two London", None, "britbox"),        # longest contained alias
    ("FX on Hulu", None, "hulu"),               # alias as a whole word
    ("Cinemax", None, None),                    # "Max" is only part of a word
    ("FXX", None, "hulu"),
    ("Smithsonian", None, "pbs"),               # the network inside a longer alias
    ("Channel 5 HD", None, None),               # a one-character alias only matches exactly
    ("5", None, "uk5"),
    ("NBC", "Seinfeld", "netflix"),             # title rule beats the network
    ("NBC", "  seinfeld ", "netflix"),
    ("NBC", "Seinfeld: The Reunion", None),     # title rules are exact, never substrings
    (None, "Friends", "max"),                   # a title rule works without a network
    ("Some Network", None, None),
    (None, None, None),
    ("", "", None),
])
def test_match(network, title, expected):
    assert match_service(SERVICES, network, title) == expected


def test_only_the_users_services_can_come_out():
    """The old built-in map answered "britbox" for any BBC network, even with no BritBox service configured."""
    assert match_service([svc("netflix", "Netflix")], "BBC One", "Doctor Who") is None
    assert match_service([], "HBO", "Friends") is None


def test_ties_keep_the_list_order():
    a, b = svc("a", "A", ["Studio"]), svc("b", "B", ["Studio"])
    assert match_service([a, b], "Studio") == "a" and match_service([b, a], "Studio") == "b"


async def test_a_title_on_a_network_with_no_service_is_added_without_one(container):
    """Adding a title whose network matched a service id that was not configured failed (FOREIGN KEY)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/shows", json={"title": "Doctor Who", "network": "BBC One"})
    assert res.status_code == 200, res.text
    assert res.json()["service_id"] is None


async def test_title_rules_are_stored_and_used(container):
    repo = container.repository
    await repo.initialize([svc("paramount_plus", "Paramount+", ["Comedy Central"], titles=["South Park"])])
    stored = await repo.get_service("paramount_plus")
    assert stored.titles == ["South Park"]
    assert await repo.match_service_by_network(None, title="South Park") == "paramount_plus"
    assert await repo.match_service_by_network("Comedy Central") == "paramount_plus"
