"""The documented, stable part of the API (docs/api.md), and the tags OpenAPI groups the routes by.

Stable means: the path, the parameters and the fields of the answer stay, and are only extended (new optional
parameters, new fields) within a major version. Every other route is for the page that ships with the app and may
change in any release. `tests/test_openapi.py` checks that each route listed here exists and is documented, and that
every route has a tag and a summary."""

from typing import Any, Dict, List, Tuple

STABLE: List[Tuple[str, str]] = [
    ("GET", "/api/health"),
    ("GET", "/api/shows"),
    ("POST", "/api/shows"),
    ("GET", "/api/shows/{show_id}"),
    ("PATCH", "/api/shows/{show_id}"),
    ("DELETE", "/api/shows/{show_id}"),
    ("POST", "/api/shows/{show_id}/increment"),
    ("POST", "/api/shows/{show_id}/progress"),
    ("GET", "/api/shows/{show_id}/episodes"),
    ("POST", "/api/shows/{show_id}/episodes/toggle"),
    ("GET", "/api/shows/{show_id}/ratings"),
    ("PUT", "/api/shows/{show_id}/ratings/{source}"),
    ("GET", "/api/rating-sources"),
    ("POST", "/api/rating-sources"),
    ("PATCH", "/api/rating-sources/{source}"),
    ("DELETE", "/api/rating-sources/{source}"),
    ("PUT", "/api/ratings/{source}"),
    ("DELETE", "/api/ratings/{source}"),
    ("DELETE", "/api/ratings/{source}/{show_id}"),
    ("GET", "/api/services"),
    ("GET", "/api/webhooks"),
    ("GET", "/api/webhooks/events"),
    ("POST", "/api/webhooks/{name}/test"),
    ("GET", "/api/export/library.csv"),
    ("GET", "/api/export/backup.db"),
    ("GET", "/api/calendar.ics"),
]

TAGS: List[Dict[str, Any]] = [
    {"name": "Library", "description": "Titles: list, add, change, delete, and watch progress."},
    {"name": "Ratings", "description": "Custom rating sources and the values you bring: a bulk import, and one title at a time."},
    {"name": "Webhooks", "description": "Event webhooks configured in config.yaml: their state, the events, a test delivery."},
    {"name": "Services", "description": "Streaming services."},
    {"name": "Export", "description": "The library as CSV and a credential-free backup of the database."},
    {"name": "Calendar", "description": "Upcoming episodes and the iCal feed."},
    {"name": "Up next", "description": "What to watch next, and library tidy-up suggestions. For the page; may change."},
    {"name": "Tonight's pick", "description": "Picks something to watch. For the page; may change."},
    {"name": "Discover", "description": "Trending titles and recommendations. For the page; may change."},
    {"name": "Planner", "description": "Month-by-month rotation plan. For the page; may change."},
    {"name": "History", "description": "Viewing over the last 7, 30 or 365 days. For the page; may change."},
    {"name": "Insights", "description": "Viewing insights. For the page; may change."},
    {"name": "Catalog", "description": "Searching the catalogue for titles to add. For the page; may change."},
    {"name": "Drivers", "description": "The drivers, and the library driver's login and sync. For the page; may change."},
    {"name": "Sync review", "description": "Review mode and the outbox. For the page; may change."},
    {"name": "Notifications", "description": "The daily digest preview and test. For the page; may change."},
    {"name": "Themes", "description": "The themes the page can show and the server's default. For the page; may change."},
    {"name": "Settings", "description": "The Settings tab: the settings, their sources and ranges. For the page; may change."},
    {"name": "System", "description": "Health, statistics and the System tab. Only /api/health is stable."},
]


def mark_stable(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Adds `x-stable: true` to the operations in STABLE (and a note in their description)."""
    wanted = {(m.lower(), p) for m, p in STABLE}
    for path, ops in schema.get("paths", {}).items():
        for method, op in ops.items():
            if (method, path) in wanted:
                op["x-stable"] = True
    return schema
