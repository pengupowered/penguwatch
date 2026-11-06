"""The events PenguWatch publishes to event webhooks (docs/extending.md), and the envelope they are sent in."""

import uuid
from typing import Any, Dict, Optional

from penguwatch import clock

# name -> what it means; the keys are the values `events:` of a webhook accepts
EVENT_TYPES: Dict[str, str] = {
    "title.added": "A title joined the library.",
    "title.status_changed": "A title's status changed (watching, completed, ...).",
    "title.rating_changed": "Your rating of a title changed, or a custom rating source's value for it.",
    "episode.watched": "Episodes of a title were marked watched in PenguWatch.",
    "episode.airs_today": "An episode of a title in your library airs today.",
    "library.imported": "The first full sync from the library driver brought titles in: one event with the count, not one per title.",
    "ratings.imported": "A bulk rating import changed values: one event per batch, not one per title.",
    "ratings.cleared": "Every value of a rating source was removed, or the source was deleted.",
    "sync.failed": "A sync run with the library driver failed (sent when the error is new, not on every retry).",
}
PAYLOAD_VERSION = 2  # 2: a title's `ids` replaced its single tracker id


def title_ref(row: Any, ids: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The small description of a title every event carries. `row` has id, title, year, media_type; `ids` are the
    title's external ids ({"imdb": "tt...", ...})."""
    return {"id": row["id"], "title": row["title"], "year": row["year"], "media_type": row["media_type"] or "show",
            "ids": dict(ids or {})}


def envelope(event_type: str, data: Dict[str, Any], event_id: Optional[str] = None) -> Dict[str, Any]:
    """The JSON body of a delivery: {version, id, type, created_at, data}."""
    return {
        "version": PAYLOAD_VERSION,
        "id": event_id or str(uuid.uuid4()),
        "type": event_type,
        "created_at": clock.utc_now().isoformat(),
        "data": data,
    }
