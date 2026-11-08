"""The MCP rules of POST /mcp: JSON-RPC 2.0 framing, the handshake, and tools/list and tools/call.

Model Context Protocol over the Streamable HTTP transport, as a stateless server that answers every POST with one
JSON object: `handle` takes the request body and the MCP-Protocol-Version header and returns a `Reply` (status and the
JSON object to send, or None for an empty body). Nothing here imports the web framework: route.py is the route,
tools.py the tools, and this module is tested with plain dicts.

Protocol revisions: SUPPORTED_VERSIONS are the initialize-handshake revisions of the reference SDK (mcp-types 2.3.0,
`HANDSHAKE_PROTOCOL_VERSIONS` in mcp_types/version.py: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25; the SDK's
`LATEST_HANDSHAKE_VERSION` is 2025-11-25). The SDK also knows a newer per-request-envelope revision (2026-07-28)
and probes for it first with `server/discover`; this server answers that probe "method not found", which makes the
client fall back to `initialize`, as the SDK documents for servers that do not speak it. `initialize` echoes the
client's version when it is supported, else answers with our latest.

Deliberately not implemented: SSE streams (no GET stream, no streamed POST answers; GET and DELETE are 405), sessions
(no Mcp-Session-Id: every request stands alone), resumability, JSON-RPC batches (-32600), prompts, resources, sampling,
logging, tool-list change notifications, cancellation and progress. Notifications and client responses are accepted
(202) and ignored. The Accept header is not enforced: the answer is always application/json, which every client that
lists it (Claude Code, the SDK) accepts.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pydantic import ValidationError

from penguwatch import __version__
from penguwatch.mcp.tools import TOOLS, Tool, ToolFailure
from penguwatch.observability import metrics

_log = logging.getLogger("penguwatch.mcp")

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "PenguWatch tracks a household's TV shows and movies and the streaming subscriptions that carry them. Everything "
    "here is read-only. Use `up_next` for what to continue watching, `tonights_pick` to choose something, "
    "`search_library` and `title` to look titles up, `schedule` for upcoming episodes, `optimizer` for streaming costs "
    "and which services to keep, and `sync_status` for the state of the tracker sync."
)


@dataclass
class Reply:
    status: int
    body: Optional[Dict[str, Any]] = None  # None: an empty body (202)


def _error(status: int, code: int, message: str, request_id: Any = None) -> Reply:
    return Reply(status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _result(request_id: Any, result: Dict[str, Any]) -> Reply:
    return Reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


def _valid_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def encode(body: Dict[str, Any]) -> bytes:
    # ensure_ascii: a lone surrogate (an id or a method name the client sent as "\\ud800") has no UTF-8 form and would be a 500
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def version_supported(header: Optional[str]) -> bool:
    """Whether an MCP-Protocol-Version header is one we speak (no header: yes, the spec says to assume an old one)."""
    return header is None or header.strip() in SUPPORTED_VERSIONS


def _listing(tool: Tool) -> Dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema,
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


def _problem(error: ValidationError) -> str:
    """The first problem in a sentence, without the value that caused it."""
    first = error.errors(include_input=False, include_url=False)[0]
    where = ".".join(str(p) for p in first["loc"])[:100]  # an unknown argument's name is the caller's text
    return f"{where + ': ' if where else ''}{first['msg']}"[:200]


def _tool_result(payload: Any, *, error: bool) -> Dict[str, Any]:
    if error:
        return {"content": [{"type": "text", "text": str(payload)}], "isError": True}
    # JSON-ready first (enums, dates), so the text and structuredContent are the same object
    plain = json.loads(json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False))
    return {"content": [{"type": "text", "text": json.dumps(plain, ensure_ascii=False, separators=(",", ":"))}],
            "structuredContent": plain, "isError": False}


async def _call_tool(container, params: Any, request_id: Any) -> Reply:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _error(200, INVALID_PARAMS, "tools/call needs a tool name.", request_id)
    tool = next((t for t in TOOLS if t.name == params["name"]), None)
    if tool is None:
        metrics.observe_mcp("unknown", "error")  # never the caller's text as a label
        return _error(200, INVALID_PARAMS, f"Unknown tool '{params['name'][:64]}'.", request_id)
    arguments = params.get("arguments")
    arguments = {} if arguments is None else arguments
    if not isinstance(arguments, dict):
        metrics.observe_mcp(tool.name, "error")
        return _error(200, INVALID_PARAMS, "Arguments must be an object.", request_id)
    try:
        parsed = tool.model.model_validate(arguments)
    except ValidationError as e:
        metrics.observe_mcp(tool.name, "error")
        return _error(200, INVALID_PARAMS, _problem(e), request_id)
    try:
        result, outcome = _tool_result(await tool.handler(container, parsed), error=False), "ok"
    except ToolFailure as e:
        result, outcome = _tool_result(str(e), error=True), "error"
    except Exception:  # a bug in a tool: logged here, the caller is told nothing of the inside
        _log.exception("MCP tool %s failed", tool.name)
        result, outcome = _tool_result("The tool failed unexpectedly; see PenguWatch's log.", error=True), "error"
    metrics.observe_mcp(tool.name, outcome)
    return _result(request_id, result)


async def handle(raw: bytes, container, version_header: Optional[str] = None) -> Reply:
    """One POST body to its answer. `container` is the app's AppContainer (the tools read it)."""
    if not version_supported(version_header):
        return _error(400, INVALID_REQUEST, "Unsupported MCP-Protocol-Version; supported: " + ", ".join(SUPPORTED_VERSIONS))
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError):
        return _error(400, PARSE_ERROR, "The body is not valid JSON.")
    if not isinstance(message, dict):
        return _error(400, INVALID_REQUEST, "Send one JSON-RPC request object (batches are not supported).")
    if message.get("jsonrpc") != "2.0":
        return _error(400, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
    method = message.get("method")
    if method is None and ("result" in message or "error" in message):
        return Reply(202)  # a response to a request of ours: we send none, and accept the message
    if not isinstance(method, str):
        return _error(400, INVALID_REQUEST, "The message has no method.",
                      message.get("id") if _valid_id(message.get("id")) else None)
    if "id" not in message:
        return Reply(202)  # a notification (initialized, cancelled, ...): nothing to answer
    request_id = message["id"]
    if not _valid_id(request_id):
        return _error(400, INVALID_REQUEST, "The id must be a string or an integer.")
    params = message.get("params")

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(request_id, {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else LATEST_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "penguwatch", "version": __version__}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": [_listing(t) for t in TOOLS]})
    if method == "tools/call":
        return await _call_tool(container, params, request_id)
    return _error(200, METHOD_NOT_FOUND, f"Method not found: {method[:64]}", request_id)
