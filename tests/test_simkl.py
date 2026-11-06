"""Tests for SIMKL Client and Sync."""

import pytest

from penguwatch.drivers.simkl import mapper
import respx
import httpx
from penguwatch.drivers.simkl.client import SimklClient, SIMKL_BASE_URL
from tests.simkl_helpers import simkl_engine
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ShowStatus, HouseholdTag


def test_simkl_client_headers():
    client = SimklClient(client_id="test_client_123", access_token="test_token_abc")
    headers = client._headers(require_auth=True)
    assert headers["simkl-api-key"] == "test_client_123"
    assert headers["Authorization"] == "Bearer test_token_abc"
    assert "PenguWatch" in headers["User-Agent"]


@pytest.mark.asyncio
@respx.mock
async def test_simkl_oauth2_device_flow():
    client = SimklClient(client_id="test_client_123")

    # Mock POST /oauth2/device
    respx.post(f"{SIMKL_BASE_URL}/oauth2/device").respond(
        status_code=200,
        json={
            "device_code": "dev123",
            "user_code": "ABC12",
            "verification_uri": "https://simkl.com/pin",
            "verification_uri_complete": "https://simkl.com/pin?user_code=ABC12",
            "expires_in": 900,
            "interval": 5,
        },
    )

    pin_res = await client.get_pin()
    assert pin_res["user_code"] == "ABC12"
    assert pin_res["device_code"] == "dev123"
    assert pin_res["auth_type"] == "oauth2_device"

    # Mock pending poll
    respx.post(f"{SIMKL_BASE_URL}/oauth2/token").respond(
        status_code=400,
        json={"error": "authorization_pending", "error_description": "User has not approved yet"},
    )
    poll_pending = await client.poll_pin_token(device_code="dev123")
    assert poll_pending["result"] == "KO"
    assert poll_pending["message"] == "authorization_pending"

    # Mock approved poll
    respx.post(f"{SIMKL_BASE_URL}/oauth2/token").respond(
        status_code=200,
        json={"access_token": "token_approved_xyz", "token_type": "Bearer"},
    )
    poll_approved = await client.poll_pin_token(device_code="dev123")
    assert poll_approved["result"] == "OK"
    assert poll_approved["access_token"] == "token_approved_xyz"


def test_simkl_parse_show_item():
    client = SimklClient(client_id="test_client_123")
    repo = DatabaseRepository(":memory:")
    sync_svc = simkl_engine(client, repo)

    raw_item = {
        "status": "watching",
        "user_rating": 9,
        "watched_episodes_count": 6,
        "show": {
            "title": "Severance",
            "year": 2022,
            "poster": "123456",
            "overview": "Mark leads a team of office workers...",
            "network": "Apple TV+",
            "genres": ["Drama", "Sci-Fi", "Thriller"],
            "total_episodes": 10,
            "aired_episodes": 9,
            "runtime": 50,
            "ids": {"simkl": 1400298, "imdb": "tt11280740"},
            "ratings": {
                "imdb": {"rating": 8.7, "votes": 210000},
                "simkl": {"rating": 8.6, "votes": 8500},
            },
        },
    }

    show = mapper.parse_show_item(raw_item)
    assert show is not None
    assert show.title == "Severance"
    assert show.year == 2022
    assert show.ids.get("simkl") == 1400298
    assert show.ids.get("imdb") == "tt11280740"
    assert show.network == "Apple TV+"
    assert show.status == ShowStatus.WATCHING
    assert show.imdb_rating == 8.7
    assert show.simkl_rating == 8.6
    assert show.watched_episodes == 6
    assert show.aired_episodes == 9
    assert show.episodes_left == 3


def test_simkl_parse_anime_item():
    client = SimklClient(client_id="test_client_123")
    repo = DatabaseRepository(":memory:")
    sync_svc = simkl_engine(client, repo)

    raw_anime = {
        "status": "watching",
        "watched_episodes_count": 24,
        "total_episodes_count": 25,
        "show": {
            "title": "Sword Art Online",
            "year": 2012,
            "ids": {"simkl": 37226, "imdb": "tt2250192"},
            "ratings": {
                "mal": {"rating": 7.2},
                "simkl": {"rating": 7.6},
            },
        },
    }

    show = mapper.parse_show_item(raw_anime, is_anime=True)
    assert show is not None
    assert show.title == "Sword Art Online"
    assert "Anime" in show.genres
    assert show.watched_episodes == 24
    assert show.total_episodes == 25
    assert show.aired_episodes == 25
    assert show.episodes_left == 1
    assert show.imdb_rating == 7.2  # MAL rating fallback
    assert show.household_tag == HouseholdTag.PERSON1
