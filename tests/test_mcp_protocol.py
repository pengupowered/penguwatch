"""POST /mcp: the JSON-RPC and MCP rules (penguwatch.mcp.protocol) and the route in front of them, on the real app
through ASGI: the handshake, notifications, errors, batches, the protocol header, GET and DELETE, off by default, the
guards (Host, Origin, token, body size) and the call counter."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import __version__
from penguwatch.config import AppConfig, EnvOverrides, load_config
from penguwatch.main import app, security_config
from penguwatch.mcp import protocol
from penguwatch.observability import metrics, render_prometheus
from tests.mcp_helpers import no_network  # noqa: F401  (fixture)

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

pytestmark = pytest.mark.usefixtures("no_network")


def rpc(method, params=None, id=1, **extra):
    message = {"jsonrpc": "2.0", "method": method, **extra}
    if id is not None:
        message["id"] = id
    if params is not None:
        message["params"] = params
    return message


@pytest.fixture
async def on(container):
    container.config.mcp.enabled = True
    return container


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def post(body, headers=None, raw=False):
    async with client() as ac:
        return await ac.post("/mcp", content=body if raw else json.dumps(body), headers={**HEADERS, **(headers or {})})


# --- off by default ---------------------------------------------------------------------------------------------------

def test_the_endpoint_is_off_by_default():
    assert AppConfig().mcp.enabled is False
    assert "enabled: false" in open("config.example.yaml").read().split("mcp:")[1][:900]


async def test_off_means_404_for_every_method(container):
    async with client() as ac:
        for method in ("POST", "GET", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"):
            res = await ac.request(method, "/mcp", content=json.dumps(rpc("ping")), headers=HEADERS)
            assert res.status_code == 404, method  # never 405 or 415: nothing says there is an endpoint here
        text = await ac.post("/mcp", content="x", headers={"Content-Type": "text/plain"})
        assert text.status_code == 404


async def test_off_before_the_app_has_started_is_404_too():
    async with client() as ac:  # no container installed
        assert (await ac.post("/mcp", json=rpc("ping"))).status_code == 404


def test_the_environment_turns_it_on_and_off(monkeypatch):
    monkeypatch.setenv("PENGUWATCH_MCP_ENABLED", "true")
    assert load_config(env=EnvOverrides()).mcp.enabled is True
    monkeypatch.setenv("PENGUWATCH_MCP_ENABLED", "")
    assert load_config(env=EnvOverrides()).mcp.enabled is False
    monkeypatch.setenv("PENGUWATCH_MCP_ENABLED", "false")
    assert load_config(env=EnvOverrides()).mcp.enabled is False


async def test_nothing_of_mcp_is_in_the_openapi_schema(on):
    async with client() as ac:
        assert "/mcp" not in (await ac.get("/openapi.json")).json()["paths"]


# --- the handshake --------------------------------------------------------------------------------------------------------

async def test_initialize_echoes_a_supported_version_and_describes_the_server(on):
    for version in protocol.SUPPORTED_VERSIONS:
        res = await post(rpc("initialize", {"protocolVersion": version, "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "1"}}))
        assert res.status_code == 200 and res.headers["content-type"] == "application/json"
        result = res.json()["result"]
        assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "penguwatch", "version": __version__}
    assert "read-only" in result["instructions"]
    assert "Mcp-Session-Id" not in res.headers and "mcp-session-id" not in res.headers  # stateless


async def test_initialize_with_an_unknown_version_answers_our_latest(on):
    for params in ({"protocolVersion": "1999-01-01"}, {}, None):
        res = await post(rpc("initialize", params))
        assert res.json()["result"]["protocolVersion"] == protocol.LATEST_VERSION


async def test_ping_and_the_tool_list(on):
    assert (await post(rpc("ping", id="abc"))).json() == {"jsonrpc": "2.0", "id": "abc", "result": {}}
    listed = (await post(rpc("tools/list"))).json()["result"]["tools"]
    assert [t["name"] for t in listed] == ["up_next", "tonights_pick", "search_library", "title", "schedule",
                                           "optimizer", "sync_status", "history"]


# --- notifications, responses and errors ------------------------------------------------------------------------------------

async def test_notifications_and_client_responses_are_accepted_with_an_empty_body(on):
    for message in (rpc("notifications/initialized", id=None), rpc("notifications/cancelled", {"requestId": 1}, id=None),
                    {"jsonrpc": "2.0", "id": 7, "result": {}},
                    {"jsonrpc": "2.0", "id": 7, "error": {"code": -1, "message": "x"}}):
        res = await post(message)
        assert res.status_code == 202 and res.content == b"", message


async def test_unknown_methods_are_method_not_found(on):
    body = (await post(rpc("resources/list", id=5))).json()
    assert body["id"] == 5 and body["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert (await post(rpc("server/discover"))).json()["error"]["code"] == protocol.METHOD_NOT_FOUND


async def test_bad_json_is_a_parse_error(on):
    res = await post(b"{not json", raw=True)
    assert res.status_code == 400 and res.json()["error"]["code"] == protocol.PARSE_ERROR and res.json()["id"] is None
    assert (await post(b"\xff\xfe", raw=True)).json()["error"]["code"] == protocol.PARSE_ERROR
    assert (await post(b"[" * 100000, raw=True)).json()["error"]["code"] == protocol.PARSE_ERROR


async def test_malformed_messages_are_invalid_requests(on):
    for message in ({"id": 1, "method": "ping"}, {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": 1, "method": 5},
                    {"jsonrpc": "2.0", "id": True, "method": "ping"}, {"jsonrpc": "2.0", "id": [1], "method": "ping"},
                    "ping", 5, None):
        res = await post(message)
        assert res.status_code == 400 and res.json()["error"]["code"] == protocol.INVALID_REQUEST, message


async def test_a_batch_is_refused(on):
    res = await post([rpc("ping"), rpc("ping", id=2)])
    assert res.status_code == 400 and res.json()["error"]["code"] == protocol.INVALID_REQUEST
    assert "batch" in res.json()["error"]["message"]


async def test_tools_call_with_bad_params_is_invalid_params(on):
    for params in (None, {}, {"name": 5}, {"name": "up_next", "arguments": []}, {"name": "no_such_tool"},
                   {"name": "up_next", "arguments": {"limit": 0}}, {"name": "up_next", "arguments": {"limit": "x"}},
                   {"name": "up_next", "arguments": {"bogus": 1}}, {"name": "title", "arguments": {}}):
        body = (await post(rpc("tools/call", params))).json()
        assert body["error"]["code"] == protocol.INVALID_PARAMS, params
        assert "result" not in body


async def test_a_tool_failure_is_an_error_result_not_a_protocol_error(on):
    body = (await post(rpc("tools/call", {"name": "title", "arguments": {"id": 4242}}))).json()
    assert body["result"]["isError"] is True and "4242" in body["result"]["content"][0]["text"]


async def test_a_crashing_tool_is_reported_without_its_inside(on, monkeypatch):
    async def boom(container, args):
        raise RuntimeError("secret detail /app/x.py")
    tool = protocol.TOOLS[0]
    monkeypatch.setattr(protocol, "TOOLS", [type(tool)(tool.name, tool.description, tool.model, boom)])
    body = (await post(rpc("tools/call", {"name": "up_next"}))).json()
    assert body["result"]["isError"] is True and "secret detail" not in json.dumps(body)


async def test_a_call_answers_with_text_and_the_same_structured_content(on):
    result = (await post(rpc("tools/call", {"name": "up_next", "arguments": {}}))).json()["result"]
    assert result["isError"] is False and result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert " " not in result["content"][0]["text"]  # compact JSON (no library, so no titles with spaces)


# --- the transport rules ------------------------------------------------------------------------------------------------------

async def test_the_protocol_version_header_is_checked(on):
    for version in protocol.SUPPORTED_VERSIONS:
        assert (await post(rpc("ping"), {"MCP-Protocol-Version": version})).status_code == 200
    assert (await post(rpc("ping"))).status_code == 200  # no header: an older client
    bad = await post(rpc("ping"), {"MCP-Protocol-Version": "2099-01-01"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == protocol.INVALID_REQUEST
    assert (await post(rpc("ping"), {"MCP-Protocol-Version": "2026-07-28"})).status_code == 400  # not spoken here


async def test_only_json_is_accepted_so_a_cross_site_page_cannot_send_it_without_a_preflight(on):
    body = json.dumps(rpc("ping"))
    for content_type in ("text/plain", "text/plain;charset=UTF-8", "application/x-www-form-urlencoded",
                         "multipart/form-data; boundary=x", "application/jsonx", "application/json-seq", ""):
        res = await post(body, {"Content-Type": content_type}, raw=True)
        assert res.status_code == 415 and res.headers["accept-post"] == "application/json", repr(content_type)
    async with client() as ac:  # no Content-Type at all
        assert (await ac.post("/mcp", content=body)).status_code == 415
    for content_type in ("application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"):
        assert (await post(body, {"Content-Type": content_type}, raw=True)).status_code == 200, content_type


async def test_answers_are_never_cached(on, monkeypatch):
    for headers in ({}, {"Origin": "http://evil.example"}, {"Host": "evil.example.com"}, {"Content-Type": "text/plain"}):
        res = await post(rpc("ping"), headers)
        assert res.headers["cache-control"] == "no-store", (headers, res.status_code)
    for method in ("GET", "DELETE"):
        async with client() as ac:
            assert (await ac.request(method, "/mcp")).headers["cache-control"] == "no-store"
    monkeypatch.setattr(security_config, "access_token", "tok")
    assert (await post(rpc("ping"))).headers["cache-control"] == "no-store"  # the 401 too


async def test_a_lone_surrogate_is_never_a_server_error(on):
    """json.loads accepts "\\ud800"; an answer that echoes it must still be encodable."""
    cases = [
        b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"method":"bad\\ud800method"}',
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"no\\udfffsuch"}}',
        b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"up_next","arguments":{"x\\ud800":1}}}',
        b'{"jsonrpc":"2.0","id":[1],"method":"ping"}',
    ]
    for raw in cases:
        res = await post(raw, raw=True)
        assert res.status_code < 500, raw
        res.json()  # a valid JSON answer, the surrogate escaped
    assert (await post(cases[0], raw=True)).json()["id"] == "\ud800"
    assert "ud800" in (await post(cases[1], raw=True)).text.lower()


async def test_the_id_error_names_the_types_it_takes(on):
    message = (await post({"jsonrpc": "2.0", "id": [1], "method": "ping"})).json()["error"]["message"]
    assert "string or an integer" in message


async def test_an_unknown_argument_name_is_echoed_only_in_part(on):
    body = (await post(rpc("tools/call", {"name": "up_next", "arguments": {"x" * 5000: 1}}))).json()
    assert body["error"]["code"] == protocol.INVALID_PARAMS and len(body["error"]["message"]) < 400


async def test_the_rate_limit_is_a_json_rpc_error_with_retry_after(on, monkeypatch):
    from penguwatch.mcp import route
    async def limited(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=429, detail="Too many requests; try again in 7s.", headers={"Retry-After": "7"})
    monkeypatch.setattr(route, "_RATE", limited)
    res = await post(rpc("ping"))
    assert res.status_code == 429 and res.headers["retry-after"] == "7"
    assert res.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": route.RATE_LIMITED, "message": "Too many requests; try again in 7s."}}


async def test_get_and_delete_are_405_with_allow_post(on):
    async with client() as ac:
        for method in ("GET", "DELETE"):
            res = await ac.request(method, "/mcp")
            assert res.status_code == 405 and res.headers["allow"] == "POST", method
        for method in ("PUT", "PATCH", "OPTIONS", "HEAD"):
            assert (await ac.request(method, "/mcp")).status_code == 405, method


async def test_a_large_answer_is_plain_json_even_with_gzip_negotiated(on):
    """GZip middleware compresses big answers when the client allows it; the body must still decode as one JSON object."""
    async with client() as ac:
        res = await ac.post("/mcp", content=json.dumps(rpc("tools/list")), headers={**HEADERS, "Accept-Encoding": "gzip"})
    assert res.status_code == 200 and res.headers.get("content-encoding") == "gzip"
    assert res.json()["result"]["tools"]  # httpx inflated it


async def test_a_body_over_the_cap_is_refused_by_the_guards(on):
    big = json.dumps(rpc("ping", {"pad": "x" * (security_config.max_body_bytes + 10)}))
    assert (await post(big, raw=True)).status_code == 413


# --- guards: Host, Origin, token ---------------------------------------------------------------------------------------------------

async def test_a_foreign_host_is_refused(on):
    res = await post(rpc("ping"), {"Host": "evil.example.com"})
    assert res.status_code == 421


async def test_a_cross_site_browser_post_is_refused_and_a_plain_client_is_not(on):
    assert (await post(rpc("ping"), {"Origin": "http://evil.example"})).status_code == 403
    assert (await post(rpc("ping"), {"Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await post(rpc("ping"), {"Origin": "http://test"})).status_code == 200  # same origin
    assert (await post(rpc("ping"))).status_code == 200  # an MCP client sends no Origin


async def test_the_endpoint_needs_the_token_when_one_is_set(on, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    none = await post(rpc("ping"))
    assert none.status_code == 401 and none.headers["www-authenticate"].startswith("Bearer")
    assert (await post(rpc("ping"), {"Authorization": "Bearer wrong"})).status_code == 401
    assert (await post(rpc("ping"), {"Authorization": "Bearer s3cret-token"})).status_code == 200
    async with client() as ac:
        assert (await ac.get("/mcp")).status_code == 401  # GET too: no probing without the token


async def test_the_token_decides_before_the_off_switch_reveals_anything(container, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    assert (await post(rpc("ping"))).status_code == 401  # off, no token: 401, not 404
    assert (await post(rpc("ping"), {"Authorization": "Bearer s3cret-token"})).status_code == 404


# --- telemetry ------------------------------------------------------------------------------------------------------------------

async def test_calls_are_counted_by_tool_and_outcome(on):
    before = dict(metrics.mcp_count)
    await post(rpc("tools/call", {"name": "sync_status"}))
    await post(rpc("tools/call", {"name": "title", "arguments": {"id": 4242}}))
    await post(rpc("tools/call", {"name": "up_next", "arguments": {"limit": 0}}))
    await post(rpc("tools/call", {"name": "made up"}))
    await post(rpc("ping"))
    delta = {k: v - before.get(k, 0) for k, v in metrics.mcp_count.items() if v - before.get(k, 0)}
    assert delta == {("sync_status", "ok"): 1, ("title", "error"): 1, ("up_next", "error"): 1, ("unknown", "error"): 1}
    text = render_prometheus(metrics, [], "t")
    assert "# TYPE penguwatch_mcp_calls_total counter" in text
    assert 'penguwatch_mcp_calls_total{tool="sync_status",outcome="ok"}' in text
    assert "made up" not in text
