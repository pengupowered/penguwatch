"""POST /mcp: the read-only MCP endpoint (docs/mcp.md). The route only reads the request and hands the body to the
protocol; every other method is 405 (no SSE stream, no sessions). While `mcp.enabled` is off every method answers 404,
decided on each request. The security middleware has already checked the Host, the Origin, the access token and the
body size, exactly as for /api/*, and set `Cache-Control: no-store` (security.py); this route adds the content type
rule and a rate limit."""

from fastapi import APIRouter, HTTPException, Request, Response

from penguwatch.mcp import protocol
from penguwatch.security import rate_limit

router = APIRouter()

_RATE = rate_limit("mcp", 120, 60)
RATE_LIMITED = -32000  # JSON-RPC's range for a server's own errors


def _container(request: Request):
    """The app's container when the endpoint is on, else None."""
    container = getattr(request.app.state, "container", None)
    return container if container is not None and container.config.mcp.enabled else None


def _json_content(request: Request) -> bool:
    """application/json, a charset allowed. Anything else (text/plain, a form) is what a cross-site page can send
    without a preflight; refusing it makes a browser ask first, and the Origin check then refuses the ask."""
    return request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


@router.post("/mcp", include_in_schema=False)
async def post_mcp(request: Request) -> Response:
    container = _container(request)
    if container is None:
        return Response(status_code=404)
    if not _json_content(request):
        return Response(status_code=415, headers={"Accept-Post": "application/json"})
    try:
        await _RATE(request)
    except HTTPException as e:  # the limit as a JSON-RPC error, which an MCP client can show
        body = protocol.encode({"jsonrpc": "2.0", "id": None, "error": {"code": RATE_LIMITED, "message": e.detail}})
        return Response(body, status_code=e.status_code, media_type="application/json", headers=e.headers)
    reply = await protocol.handle(await request.body(), container, request.headers.get("mcp-protocol-version"))
    if reply.body is None:
        return Response(status_code=reply.status)
    return Response(protocol.encode(reply.body), status_code=reply.status, media_type="application/json")


@router.api_route("/mcp", methods=["GET", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"], include_in_schema=False)
async def other_mcp(request: Request) -> Response:
    if _container(request) is None:
        return Response(status_code=404)
    return Response(status_code=405, headers={"Allow": "POST"})
