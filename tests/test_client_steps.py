"""The steps SimklClient._request is made of (token, transport, retry policy), each on its own."""

import httpx
import pytest

from penguwatch.drivers.simkl.client import SimklClient


def answer(status, body=b"", headers=None):
    return httpx.Response(status, content=body, headers=headers or {})


@pytest.fixture
def client():
    c = SimklClient(client_id="id", access_token="tok")
    c.retry_base_delay = 2.0
    return c


@pytest.mark.parametrize("status, body, attempt, expected", [
    (502, "", 1, 2.0), (503, "", 1, 2.0), (503, "", 3, 6.0), (504, "", 1, 2.0), (429, "", 1, 2.0), (500, "", 2, 4.0),
    (400, '{"error":"rate_limit"}', 1, 2.0),   # SIMKL's per-user write lock
    (400, "bad request", 1, None), (404, "", 1, None), (403, "", 1, None),
])
def test_which_answers_are_retried_and_after_how_long(client, status, body, attempt, expected):
    assert client._retry_delay(answer(status), body, attempt) == expected


def test_retry_after_is_honoured_up_to_thirty_seconds(client):
    assert client._retry_delay(answer(429, headers={"Retry-After": "7"}), "", 1) == 7.0
    assert client._retry_delay(answer(429, headers={"Retry-After": "600"}), "", 1) == 30.0
    assert client._retry_delay(answer(429, headers={"Retry-After": "soon"}), "", 1) == 2.0


def test_a_success_body_that_is_empty_or_not_json_is_an_empty_object():
    assert SimklClient._decode(answer(204)) == {}
    assert SimklClient._decode(answer(200, b"<html>")) == {}
    assert SimklClient._decode(answer(200, b'{"a": 1}')) == {"a": 1}


async def test_an_unexpired_token_is_sent_as_it_is(client):
    assert await client._renew_if_expired(True) == "tok"
