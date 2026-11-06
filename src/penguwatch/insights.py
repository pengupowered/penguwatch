"""Pure shaping of viewing statistics for the Insights tab (no I/O, no repository)."""

import json
from datetime import date
from typing import Any, Dict, List


def _month_buckets(first: date, months: int) -> Dict[str, Dict[str, float]]:
    buckets: Dict[str, Dict[str, float]] = {}
    y, m = first.year, first.month
    for _ in range(months):
        buckets[f"{y:04d}-{m:02d}"] = {"episodes": 0, "hours": 0.0}
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return buckets


def _fold_months(buckets: Dict[str, Dict[str, float]], rows: List[Dict[str, Any]]) -> None:
    for r in rows:
        if r["ym"] in buckets:
            buckets[r["ym"]]["episodes"] += r["n"]
            buckets[r["ym"]]["hours"] += r["hours"] or 0.0


def _genre_hours(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for r in rows:
        try:
            genres = [g for g in json.loads(r["genres_json"] or "[]") if g]
        except ValueError:
            genres = []
        for g in genres:
            out[g] = out.get(g, 0.0) + (r["hours"] or 0.0)
    return out


def _weekday_counts(rows: List[Dict[str, Any]]) -> List[int]:
    weekdays = [0] * 7  # Sunday = 0 (SQLite %w)
    for r in rows:
        if r["dow"] is not None:
            weekdays[r["dow"]] = r["n"]
    return weekdays


def build_insights(rows: Dict[str, Any], months: int) -> Dict[str, Any]:
    """Turns the raw aggregates from ReportsMixin.get_insight_rows() into the Insights payload.

    Episode rows are folded before movie rows so the float sums stay in a fixed order."""
    buckets = _month_buckets(date.fromisoformat(rows["first"]), months)
    _fold_months(buckets, rows["episode_months"])
    _fold_months(buckets, rows["movie_months"])
    genre_hours = _genre_hours(rows["genre_rows"])
    statuses = rows["statuses"]
    finished = statuses.get("completed", 0)
    abandoned = statuses.get("dropped", 0)
    return {
        "months": [{"month": k, **v, "hours": round(v["hours"], 1)} for k, v in buckets.items()],
        "by_service": rows["by_service"],
        "genres": sorted(({"genre": g, "hours": round(h, 1)} for g, h in genre_hours.items()), key=lambda x: -x["hours"])[:10],
        "weekdays": _weekday_counts(rows["weekday_rows"]),
        "active_days_90d": rows["active_days"],
        "completion_rate": round(finished / (finished + abandoned), 3) if (finished + abandoned) else None,
        "statuses": statuses,
    }


def api_payload(data: Dict[str, Any], service_names: Dict[str, str]) -> Dict[str, Any]:
    """Adds the service names and totals the /api/insights endpoint returns (mutates and returns `data`)."""
    data["by_service"] = sorted(
        ({"service_id": k, "service": service_names.get(k, "Unassigned" if not k else k), **v, "hours": round(v["hours"], 1)}
         for k, v in data["by_service"].items()),
        key=lambda r: -r["hours"],
    )
    data["total_hours"] = round(sum(m["hours"] for m in data["months"]), 1)
    data["total_episodes"] = sum(m["episodes"] for m in data["months"])
    return data
