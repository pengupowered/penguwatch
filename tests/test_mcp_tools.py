"""The MCP tools on a seeded library (tests/mcp_helpers.py): what each returns, the limits, and that no tool output
holds a secret."""

import json
import random
import re

import pytest

from penguwatch.mcp import protocol, tools
from penguwatch.sync import summary
from penguwatch.models import MediaType, RandomPickFilter, Show, ShowStatus
from tests.mcp_helpers import MARKERS, SECRETS, TOOL_CALLS, no_network, seed  # noqa: F401  (fixture)


pytestmark = pytest.mark.usefixtures("no_network")


@pytest.fixture
async def library(container, monkeypatch, tmp_path):
    container.config.mcp.enabled = True
    await seed(container, monkeypatch, tmp_path)
    return container


async def call(container, name, arguments=None):
    reply = await protocol.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": arguments or {}}}).encode(), container)
    return reply.body


async def data(container, name, arguments=None):
    body = await call(container, name, arguments)
    assert "error" not in body, body
    assert not body["result"]["isError"], body
    return body["result"]["structuredContent"]


def today_iso():
    from penguwatch import clock
    return clock.today().isoformat()


async def test_up_next_lists_the_shows_in_progress_with_their_next_episode(library):
    out = await data(library, "up_next")
    assert out["total"] == 2
    first = out["up_next"][0]
    assert first["title"] in ("Severance", "Andor") and first["next_to_watch"]["code"]
    severance = next(s for s in out["up_next"] if s["title"] == "Severance")
    assert severance["watched"] == 2 and severance["aired"] == 4 and severance["episodes_left"] == 2
    # the next episode to press play on (aired), and the next one that has not aired yet: two different episodes
    assert severance["next_to_watch"]["code"] == "S01E03" and severance["next_to_watch"]["title"] == "Chapter 3"
    assert severance["next_to_watch"]["air_date"] < today_iso()
    assert severance["next_new_episode"]["code"] == "S01E05" and severance["next_new_episode"]["air_date"] > today_iso()
    assert "next_episode" not in severance and "next_air" not in severance
    assert severance["for"] == "Both" and severance["service"] == "Apple TV+" and severance["service_active"] is True
    assert (await data(library, "up_next", {"limit": 1}))["shown"] == 1
    active = await data(library, "up_next", {"only_active_services": True})
    assert [s["title"] for s in active["up_next"]] == ["Severance"]  # Netflix is off in the fixture


async def test_up_next_is_the_api_list(library):
    api = await library.repository.get_up_next()
    out = await data(library, "up_next", {"limit": 60})
    assert [s["id"] for s in out["up_next"]] == [e["id"] for e in api]


async def test_tonights_pick_draws_among_open_titles_and_records_nothing(library):
    seen = set()
    for _ in range(40):
        out = await data(library, "tonights_pick", {"only_active_services": False})
        seen.add(out["pick"]["title"])
        assert out["matching"] == 3  # not Old Show (completed)
    assert seen <= {"Severance", "Andor", "Heat"} and len(seen) >= 2
    default = await data(library, "tonights_pick")  # like the API, only services that are on: Apple TV+
    assert default["pick"]["title"] == "Severance" and default["matching"] == 1
    only = await data(library, "tonights_pick", {"media_type": "movie", "only_active_services": False})
    assert only["pick"]["title"] == "Heat"
    none = await data(library, "tonights_pick", {"genre": "Western"})
    assert none["pick"] is None and none["matching"] == 0
    assert (await library.repository.get_show_by_id(only["pick"]["id"])).status.value == "plantowatch"


async def test_the_pick_logic_is_shared_with_the_route(library):
    shows = await library.repository.get_shows()
    filters = RandomPickFilter(only_active_services=False)
    one = tools.pick.pick(shows, filters, rng=random.Random(5))
    two = tools.pick.pick(shows, filters, rng=random.Random(5))
    assert one["result"].id == two["result"].id and one["total_matching"] == 3


async def test_tonights_pick_refuses_unknown_arguments(library):
    body = await call(library, "tonights_pick", {"mood": "cozy"})
    assert body["error"]["code"] == protocol.INVALID_PARAMS
    body = await call(library, "tonights_pick", {"min_rating": "high"})
    assert body["error"]["code"] == protocol.INVALID_PARAMS and "high" not in body["error"]["message"]


async def test_search_library_finds_titles_in_the_library(library):
    out = await data(library, "search_library", {"query": "sever"})
    assert [t["title"] for t in out["titles"]] == ["Severance"]
    assert out["titles"][0]["status"] == "watching" and out["titles"][0]["imdb"] == 8.7
    assert (await data(library, "search_library", {"query": "zzz"}))["matches"] == 0
    done = await data(library, "search_library", {"query": "o", "status": "completed"})
    assert [t["title"] for t in done["titles"]] == ["Old Show"]
    limited = await data(library, "search_library", {"query": "e", "limit": 1})
    assert limited["shown"] == 1 and limited["matches"] > 1
    assert (await call(library, "search_library", {"query": "e", "limit": 101}))["error"]["code"] == protocol.INVALID_PARAMS
    assert (await call(library, "search_library", {}))["error"]["code"] == protocol.INVALID_PARAMS


async def test_title_by_id_and_by_name(library):
    by_name = await data(library, "title", {"name": "severance"})
    by_id = await data(library, "title", {"id": by_name["id"]})
    assert by_name == by_id
    assert by_name["watched"] == 2 and by_name["total"] == 5 and by_name["priority"] == "high"
    assert by_name["ratings"]["imdb"] == 8.7 and by_name["ratings"]["mine"] == 9.0
    assert by_name["next_to_watch"] == {"code": "S01E03", "title": "Chapter 3", "air_date": by_name["next_to_watch"]["air_date"]}
    assert by_name["next_new_episode"]["code"] == "S01E05" and by_name["next_new_episode"]["title"] == "Chapter 5"
    assert by_name["service"] == {"id": "apple_tv", "name": "Apple TV+", "monthly_cost": 9.99, "subscribed": True}
    assert by_name["genres"] == ["Drama"] and by_name["overview"] == "Severance is invented."
    ambiguous = await data(library, "title", {"name": "e"})
    assert ambiguous["other_matches"]


async def test_title_failures_are_tool_errors_and_bad_arguments_are_protocol_errors(library):
    body = await call(library, "title", {"id": 99999})
    assert body["result"]["isError"] is True and "99999" in body["result"]["content"][0]["text"]
    body = await call(library, "title", {"name": "Nonexistent"})
    assert body["result"]["isError"] is True
    assert (await call(library, "title", {}))["error"]["code"] == protocol.INVALID_PARAMS
    assert (await call(library, "title", {"id": 1, "name": "x"}))["error"]["code"] == protocol.INVALID_PARAMS


async def test_schedule_lists_upcoming_episodes_and_follows_the_driver_setting(library):
    out = await data(library, "schedule", {"days_ahead": 14})
    assert out["enabled"] is True
    assert [(e["title"], e["season"], e["episode"]) for e in out["episodes"]] == [("Severance", 1, 5)]
    assert out["episodes"][0]["service"] == "Apple TV+"
    assert (await data(library, "schedule", {"days_ahead": 1}))["episodes"] == []
    assert (await call(library, "schedule", {"days_ahead": 91}))["error"]["code"] == protocol.INVALID_PARAMS
    library.drivers.schedules = []
    off = await data(library, "schedule")
    assert off["enabled"] is False and off["episodes"] == []


async def test_schedule_times_are_in_the_household_zone(library):
    assert tools.local_time("2026-11-07") == "2026-11-07"
    from penguwatch import clock
    clock.configure("America/New_York")
    try:
        assert tools.local_time("2026-11-07T01:30:00Z") == "2026-11-06T20:30:00-05:00"
        assert tools.local_time("2026-11-07T01:30:00+00:00") == "2026-11-06T20:30:00-05:00"
        assert tools.local_time("not a time T") == "not a time T"
    finally:
        clock.configure("")


async def test_optimizer_gives_the_plan_and_the_rotation(library):
    plan = await library.optimizer.calculate_plan()
    out = await data(library, "optimizer")
    assert out["monthly_spend"] == plan.current_monthly_spend
    assert {s["id"] for s in out["services"]} == {"apple_tv", "netflix"}
    assert out["rotation"]["months"] == 6 and {s["id"] for s in out["rotation"]["services"]} == {"apple_tv", "netflix"}
    assert "rotation" not in await data(library, "optimizer", {"rotation_months": 0})
    assert len((await data(library, "optimizer", {"limit": 1}))["services"]) == 1


async def test_sync_status_counts_and_has_only_the_whitelisted_fields(library):
    out = await data(library, "sync_status")
    assert out["driver"] == "simkl" and out["remote"] is True
    assert out["last_sync"] == "2026-11-05T20:30:00+00:00"
    assert set(out) == {"driver", "label", "remote", "connected", "configured", "auth_state", "review_mode", "last_sync",
                        "last_error", "last_result", "pending_changes", "drivers"}
    assert out["auth_state"] == "needs_reconnect"  # said by the state, not by the driver's message
    assert out["last_error"] == {"kind": "auth", "http_status": 401, "text": summary.ERROR_SENTENCES["auth"]}
    assert set(out["pending_changes"]) == {"outgoing_waiting_for_approval", "outgoing_ready_to_send",
                                           "outgoing_failing", "incoming_to_review"}
    assert out["drivers"]["library"] == "simkl"


async def test_history_is_the_apis_payload_cut_down(library):
    # the seeded library marks 5 episodes (Severance 2, Andor 1, Old Show 2) with one exact stamp two days ago: a bulk mark
    api = await library.repository.get_history(30, False)
    out = await data(library, "history")
    assert out["period"] == {k: api["period"][k] for k in ("days", "tz", "start", "end", "previous_start")} and out["period"]["days"] == 30
    assert out["totals"] == api["totals"] and out["totals"]["current"]["episodes"] == 0
    assert (out["bulk_episodes"], out["bulk_groups"], out["include_bulk"]) == (5, 1, False)
    assert "daily" not in out and out["top_shows"] == [] and "weekdays" not in out and "series" not in out
    with_bulk = await data(library, "history", {"include_bulk": True})
    assert with_bulk["totals"]["current"]["episodes"] == 5 and with_bulk["include_bulk"] is True
    shows = with_bulk["top_shows"]
    assert {t["title"] for t in shows} == {"Severance", "Andor", "Old Show"} and all(set(t) == {"id", "title", "hours", "episodes"} for t in shows)
    assert [t["title"] for t in shows] == [t["title"] for t in (await library.repository.get_history(30, True))["top_shows"]][:5]
    assert [g["genre"] for g in with_bulk["genres"]] == ["Drama"]
    assert {r["service"] for r in with_bulk["by_service"]} >= {"Netflix", "Apple TV+"}
    assert "poster" not in json.dumps(with_bulk) and "data:image" not in json.dumps(with_bulk)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT00:00:00[+-]\d\d:\d\d", out["period"]["start"])  # ISO, with the zone's offset


async def test_history_reads_the_bulk_threshold_setting_live(library):
    # the seeded bulk mark is one group of 5 episodes: a threshold above 5 makes it ordinary viewing
    library.config.history.bulk_min = 6
    assert (await data(library, "history"))["bulk_episodes"] == 0
    library.config.history.bulk_min = 5
    assert (await data(library, "history"))["bulk_episodes"] == 5


async def test_history_gives_the_daily_series_for_seven_days_only(library):
    week = await data(library, "history", {"days": 7, "include_bulk": True})
    assert [d["key"] for d in week["daily"]][-1] == today_iso() and len(week["daily"]) == 7
    assert sum(d["episodes"] for d in week["daily"]) == 5 == week["totals"]["current"]["episodes"]
    for days in (30, 365):
        assert "daily" not in await data(library, "history", {"days": days})
    assert len(json.dumps(await data(library, "history", {"days": 365}))) < 4000


async def test_history_arguments_are_strict(library):
    for arguments in ({"days": 14}, {"days": "7"}, {"days": True}, {"days": 7.0}, {"include_bulk": "yes"}, {"include_bulk": 1},
                      {"bogus": 1}):
        body = await call(library, "history", arguments)
        assert body["error"]["code"] == -32602, (arguments, body)
    schema = next(t for t in tools.TOOLS if t.name == "history").input_schema
    assert schema["properties"]["days"]["enum"] == [7, 30, 365]


async def test_every_tool_is_listed_read_only_with_a_schema(library):
    reply = await protocol.handle(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', library)
    listed = reply.body["result"]["tools"]
    assert [t["name"] for t in listed] == [t.name for t in tools.TOOLS]
    for t in listed:
        assert t["annotations"]["readOnlyHint"] is True and t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False and len(t["description"]) > 40


async def test_no_tool_output_holds_a_secret(library):
    assert {n for n, _ in TOOL_CALLS} == {t.name for t in tools.TOOLS}  # a new tool must be added to the list
    for name, arguments in TOOL_CALLS:
        body = await call(library, name, arguments)
        text = json.dumps(body)
        assert "error" not in body and not body["result"]["isError"], (name, body)
        for label, secret in SECRETS.items():
            assert secret not in text, (name, label)
        assert not [m for m in MARKERS if m in text], name


async def test_sync_status_reads_stored_state_only_and_calls_no_service(library, no_network):
    """Not even with an expired login: no account call, no renewal, nothing written to the stored tokens."""
    client = library.drivers.by_name["simkl"].client
    client.token_expires_at = 1.0  # long expired
    calls = []

    async def forbidden(*args, **kw):
        calls.append("called")
        raise AssertionError("sync_status reached for the service")

    for name in ("_request", "_send", "get_user_settings", "renew_if_due", "refresh_access_token", "_refresh", "_exchange"):
        monkeypatch_attr(client, name, forbidden)
    keys = ["driver.simkl.access_token", "driver.simkl.refresh_token", "driver.simkl.token_expires_at"]
    before = [await library.repository.get_state(k) for k in keys]
    for _ in range(3):
        out = await data(library, "sync_status")
    assert calls == [] and no_network == []
    assert out["connected"] is True
    assert [await library.repository.get_state(k) for k in keys] == before  # no token written


def monkeypatch_attr(obj, name, value):
    setattr(obj, name, value)  # the fixture's client is thrown away with the test


async def test_the_last_error_is_a_category_and_a_fixed_sentence_whatever_the_service_said(library):
    key = library.sync.state_key("last_error")
    cases = {
        "SIMKL HTTP 0: network error: [Errno -3] Temporary failure in name resolution": ("network", None),
        "SIMKL HTTP 503: upstream down at https://api.simkl.com/x?client_id=CANARY-q": ("upstream_http", 503),
        "SIMKL HTTP 401: invalid token CANARY-t": ("auth", 401),
        "something odd happened with CANARY-o": ("other", None),
    }
    for message, (kind, status) in cases.items():
        await library.repository.set_state(key, message)
        err = (await data(library, "sync_status"))["last_error"]
        assert err["kind"] == kind and err.get("http_status") == status, message
        assert err["text"] == summary.ERROR_SENTENCES[kind] and "CANARY" not in json.dumps(err)
    await library.repository.delete_state(key)
    assert "last_error" not in await data(library, "sync_status")


async def test_a_driver_without_local_status_is_not_asked_for_its_status(library):
    """status() of a driver may call its service: a driver that has no local_status is reported from is_connected()."""
    class Remote:
        name, label, remote = "other", "Other", False
        def is_connected(self): return True
        async def status(self): raise AssertionError("status() may call the service")
    out = await summary.library_summary(type("S", (), {
        "library": Remote(), "last_result": {}, "review_mode": library.sync.review_mode,
        "get_state": library.sync.get_state, "get_incoming": library.sync.get_incoming})(), library.repository)
    assert out["auth_state"] == "unknown" and out["connected"] is True


async def test_integer_arguments_are_integers_within_bounds(library):
    for name, arguments in (("search_library", {"query": "a", "limit": True}), ("up_next", {"limit": True}),
                            ("up_next", {"limit": "5"}), ("up_next", {"limit": 1.5}), ("schedule", {"days_ahead": True}),
                            ("optimizer", {"rotation_months": True}), ("title", {"id": True}), ("title", {"id": 0}),
                            ("title", {"id": 2**63}), ("title", {"id": -5})):
        assert (await call(library, name, arguments))["error"]["code"] == protocol.INVALID_PARAMS, (name, arguments)
    assert (await call(library, "title", {"id": 2**63 - 1}))["result"]["isError"] is True  # in bounds: just not there


async def test_search_treats_percent_and_underscore_as_letters(library):
    await library.repository.upsert_show(Show(title="100% Wolf", media_type=MediaType.SHOW))
    await library.repository.upsert_show(Show(title="A_B Show", media_type=MediaType.SHOW))
    for query, expected in (("%", ["100% Wolf"]), ("_", ["A_B Show"]), ("100%", ["100% Wolf"]), ("a_b", ["A_B Show"])):
        found = await data(library, "search_library", {"query": query})
        assert sorted(t["title"] for t in found["titles"]) == expected, query
    assert (await data(library, "search_library", {"query": "\\"}))["matches"] == 0


async def test_up_next_total_is_the_real_count_beyond_the_apis_cap(library):
    for n in range(65):
        await library.repository.upsert_show(Show(title=f"Bulk {n}", status=ShowStatus.WATCHING, media_type=MediaType.SHOW))
    out = await data(library, "up_next", {"limit": 60})
    assert out["total"] == 67 and out["shown"] == 60
