"""POST /mcp against the official MCP SDK client (the mcp package, a test dependency only): the real app on a real
port, the handshake (the SDK's default tries `server/discover` first, then falls back to `initialize`), tools/list and
every tool, once open and once with a bearer token. The proof that the server written here speaks the protocol, that no
tool output holds a secret, and that the GZip middleware leaves the JSON answers alone."""

import asyncio
import json
import socket

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from penguwatch.main import app, security_config
from tests.mcp_helpers import MARKERS, TOKEN, TOOL_CALLS, no_network, seed  # noqa: F401  (fixture)

pytestmark = [pytest.mark.filterwarnings("ignore"), pytest.mark.usefixtures("no_network")]


@pytest.fixture
async def server(container, monkeypatch, tmp_path):
    """The app (with the seeded container; no lifespan, which would replace it) served by uvicorn on a free port."""
    container.config.mcp.enabled = True
    await seed(container, monkeypatch, tmp_path)
    monkeypatch.setattr(security_config, "access_token", "")  # open unless a test sets a token
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_config=None))
    task = asyncio.create_task(uv.serve())
    for _ in range(200):
        if uv.started:
            break
        await asyncio.sleep(0.05)
    assert uv.started
    yield f"http://127.0.0.1:{port}/mcp"
    uv.should_exit = True
    await task


def _client(url, token=None, mode="auto"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers, timeout=60)), mode=mode)


async def _exercise(client):
    assert client.server_info.name == "penguwatch"
    tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == [n for n, _ in TOOL_CALLS]
    assert all(t.annotations.read_only_hint is True and t.annotations.open_world_hint is False for t in tools)
    for name, arguments in TOOL_CALLS:
        result = await client.call_tool(name, arguments)
        assert not result.is_error, (name, result.content)
        assert result.structured_content, name
        assert json.loads(result.content[0].text) == result.structured_content
        text = json.dumps(result.structured_content) + result.content[0].text
        assert not [m for m in MARKERS if m in text], name
    pick = (await client.call_tool("up_next", {})).structured_content
    assert {s["title"] for s in pick["up_next"]} == {"Severance", "Andor"}
    failed = await client.call_tool("title", {"id": 424242})
    assert failed.is_error and "424242" in failed.content[0].text
    with pytest.raises(MCPError):
        await client.call_tool("up_next", {"limit": 0})
    with pytest.raises(MCPError):
        await client.call_tool("no_such_tool", {})


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_sdk_client_talks_to_the_open_endpoint(server, mode):
    async with _client(server, mode=mode) as client:
        await _exercise(client)


async def test_the_sdk_client_with_a_bearer_token(server, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    async with _client(server, TOKEN) as client:
        await _exercise(client)
    for token in (None, "wrong-token"):
        with pytest.raises(Exception):
            async with _client(server, token):  # the handshake is refused
                pass
    async with httpx2.AsyncClient() as plain:
        assert (await plain.post(server, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
        ok = await plain.post(server, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and ok.json()["result"] == {}


async def test_big_answers_decode_with_gzip_on_a_real_connection(server):
    async with httpx2.AsyncClient(headers={"Accept-Encoding": "gzip"}) as http:
        res = await http.post(server, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              headers={"Accept": "application/json, text/event-stream"})
    assert res.headers["content-encoding"] == "gzip" and res.headers["content-type"] == "application/json"
    assert len(res.json()["result"]["tools"]) == len(TOOL_CALLS)
