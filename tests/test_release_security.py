"""Security fixes from the release review: no forwarded client address, no notification URL in errors, no bare CR in iCal."""

from datetime import timedelta
from unittest import mock

import httpx
import respx
from httpx import ASGITransport, AsyncClient

from penguwatch import clock
from penguwatch.main import app
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.notify import Notifier

SECRET_TOPIC = "https://ntfy.example/secret-topic-7f3a9c"


@respx.mock
async def test_notify_test_never_answers_with_the_notification_url(container):
    """POST /api/notify/test: the receiver answers an error; the API must not echo the URL (an ntfy topic URL is a
    secret: it is kept out of logs on purpose, notify.deliver_new)."""
    respx.post(SECRET_TOPIC).mock(return_value=httpx.Response(500))
    container.notifier = Notifier(SECRET_TOPIC, "ntfy")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/notify/test")
    assert res.status_code == 502
    assert "secret-topic-7f3a9c" not in res.text, res.text


def test_server_does_not_trust_forwarded_headers():
    """docs/security.md: "it does not read X-Forwarded-For". uvicorn trusts X-Forwarded-For from 127.0.0.1 unless told
    otherwise, which rewrites the client address the sign-in throttle and the rate limits key on."""
    from penguwatch import main as app_main
    with mock.patch.object(app_main.uvicorn, "run") as run:
        app_main.main()
    kwargs = run.call_args.kwargs
    assert kwargs.get("proxy_headers") is False or kwargs.get("forwarded_allow_ips") == "", kwargs


async def test_calendar_feed_has_no_bare_carriage_return(container):
    """A title (from a catalogue, the API or an import) with a carriage return must not break an iCal content line."""
    repo = container.repository
    await repo.upsert_show(Show(
        title="Evil\rEND:VEVENT\rBEGIN:VEVENT\rSUMMARY:Injected", ids={"simkl": 77}, status=ShowStatus.WATCHING,
        next_episode_air_date=(clock.today() + timedelta(days=2)).isoformat(), next_episode_season=1,
        next_episode_number=2, aired_episodes=1, watched_episodes=1,
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        body = (await ac.get("/api/calendar.ics")).content
    assert b"Evil" in body
    assert b"\r" not in body.replace(b"\r\n", b""), body


def test_a_calendar_key_in_a_log_line_is_redacted():
    from penguwatch.observability import redact
    assert redact("GET /api/calendar.ics?key=abc123def456") == "GET /api/calendar.ics?key=***"
    assert redact("sorted by key=title") == "sorted by key=***"  # harmless over-redaction is fine


async def test_a_sequel_year_that_is_not_a_number_does_not_break_tidy(container):
    repo = container.repository
    sid = await repo.upsert_show(Show(title="Parent", ids={"simkl": 501}, status=ShowStatus.COMPLETED, is_ended=True,
                                      media_type=MediaType.ANIME))  # sequel hints are for anime
    await repo.update_show_fields(sid, {"sequels_json": '[{"ids": {"simkl": 502}, "title": "Next", "year": "soon"},'
                                                        ' {"ids": {"simkl": 503}, "title": "Later", "year": "2001"}]'})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/tidy?months=6")
    assert res.status_code == 200, res.text
    sequels = {r["sequel"]["title"]: r["state"] for r in res.json()["returning"] if r["kind"] == "sequel"}
    assert sequels == {"Next": "upcoming", "Later": "available"}  # "soon" is no year; "2001" counts as one
