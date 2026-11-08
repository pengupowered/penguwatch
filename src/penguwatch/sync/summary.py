"""A short, credential-free, read-only summary of the library driver's sync state (used by the MCP `sync_status` tool).

GET /api/drivers/{driver}/status shows the driver's own `details` for the page and may ask the service for account
details (which can renew and store a login); this one is built from an explicit list of fields, from stored and
in-memory state only, and never spreads `status.details`. It carries no free text from the driver or the service
either: a message or an error can quote an address or a login, so the last error is reduced to a category, an HTTP
status code and a fixed sentence.
"""

import re
from typing import Any, Dict, Optional

from penguwatch.db.repository import DatabaseRepository
from penguwatch.sync.engine import SyncEngine

RESULT_FIELDS = ("status", "mode", "pushed", "updated", "enriched", "duration_s", "trigger")
_WORD = re.compile(r"^[a-z][a-z_ -]{0,23}$")
_HTTP = re.compile(r"\bHTTP (\d{3})\b")
_AUTH = re.compile(r"(?i)\b(401|403)\b|unauthori[sz]ed|not authenticated|reconnect|token|login|credential|forbidden")
_NETWORK = re.compile(r"(?i)\bHTTP 0\b|network|name resolution|timed? ?out|connection|unreachable|refused")

ERROR_SENTENCES = {
    "auth": "The library service refused the login; reconnect it on the Sync tab.",
    "network": "The library service could not be reached.",
    "upstream_http": "The library service answered with an error.",
    "other": "The last sync failed; see PenguWatch's log or the Sync tab for the reason.",
}


def error_category(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The last error as {kind, http_status?, text}: a category and a fixed sentence, none of the original wording."""
    if not text:
        return None
    http = _HTTP.search(text)
    if _AUTH.search(text):
        kind = "auth"
    elif _NETWORK.search(text):
        kind = "network"
    elif http:
        kind = "upstream_http"
    else:
        kind = "other"
    out: Dict[str, Any] = {"kind": kind}
    if http and kind in ("auth", "upstream_http"):
        out["http_status"] = int(http.group(1))
    out["text"] = ERROR_SENTENCES[kind]
    return out


def _result_value(value: Any) -> Any:
    """A number as it is, a short lower-case word (ok, error, full, manual) as it is, anything else dropped."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return value if isinstance(value, str) and _WORD.match(value) else None


async def library_summary(sync: SyncEngine, repo: DatabaseRepository) -> Dict[str, Any]:
    library = sync.library
    local = getattr(library, "local_status", None)  # a driver's status() may call the service: only local_status is used
    if local is not None:
        try:
            status = await local()
            login: Dict[str, Any] = {"connected": status.connected, "configured": status.configured,
                                     "auth_state": status.auth_state}
        except Exception:
            login = {"connected": library.is_connected(), "auth_state": "unknown"}
    else:
        login = {"connected": library.is_connected(), "auth_state": "unknown"}
    outbox = [o for o in await repo.get_outbox() if o.get("driver") == library.name]
    incoming = len((await sync.get_incoming())["entries"]) if library.remote else 0
    result = {k: _result_value(sync.last_result[k]) for k in RESULT_FIELDS if k in (sync.last_result or {})}
    return {
        "driver": library.name, "label": library.label, "remote": bool(library.remote), **login,
        "review_mode": await sync.review_mode(),
        "last_sync": await sync.get_state("last_sync"),
        "last_error": error_category(await sync.get_state("last_error")),
        "last_result": {k: v for k, v in result.items() if v is not None},
        "pending_changes": {
            "outgoing_waiting_for_approval": sum(1 for o in outbox if not o["approved"]),
            "outgoing_ready_to_send": sum(1 for o in outbox if o["approved"]),
            "outgoing_failing": sum(1 for o in outbox if o["approved"] and o.get("attempts")),
            "incoming_to_review": incoming,
        },
    }
