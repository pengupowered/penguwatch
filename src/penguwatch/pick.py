"""Tonight's Pick: which titles qualify and how likely each is, shared by POST /api/decide/pick and the MCP tool.

Pure functions of a list of shows and a filter; the caller fetches the shows (the repository applies the service,
status, household and media-type filters) and nothing here writes anything.
"""

import random
from typing import Any, Dict, List, Optional

from penguwatch.models import HouseholdTag, MediaType, Priority, RandomPickFilter, Show, ShowStatus


def candidates(shows: List[Show], filters: RandomPickFilter) -> List[Show]:
    """The shows that could be tonight's pick: open, with something to watch, and passing the filter's rating,
    binge and genre conditions."""
    found = [s for s in shows if s.status not in (ShowStatus.COMPLETED, ShowStatus.DROPPED)]
    # Shows that are caught up have nothing to watch tonight
    found = [s for s in found if s.media_type == MediaType.MOVIE or s.episodes_left > 0 or s.aired_episodes == 0]
    if filters.min_rating:
        found = [s for s in found if (s.imdb_rating or 0.0) >= filters.min_rating]
    if filters.ready_to_binge_only:
        found = [s for s in found if s.is_ready_to_binge]
    if filters.genre:
        g_lower = filters.genre.lower()
        found = [s for s in found if any(g_lower in g.lower() for g in s.genres)]
    return found


def weight(show: Show) -> float:
    w = 1.0
    if show.household_tag == HouseholdTag.BOTH:
        w *= 3.0
    if show.priority == Priority.HIGH:
        w *= 3.0
    elif show.priority == Priority.MEDIUM:
        w *= 1.5
    if show.status == ShowStatus.WATCHING:
        w *= 2.0
    if show.imdb_rating:
        w *= 1.0 + max(0.0, show.imdb_rating - 7.0) * 0.25
    return w


def pick(shows: List[Show], filters: RandomPickFilter, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """The route's answer: {"result": <Show>, "total_matching": n, "message": ...}, or result None when nothing matches."""
    found = candidates(shows, filters)
    if not found:
        return {"result": None, "message": "No shows found matching the selected criteria."}
    chooser = rng.choices if rng else random.choices
    selected = chooser(found, weights=[weight(s) for s in found], k=1)[0]
    return {"result": selected, "total_matching": len(found), "message": f"Tonight's Pick: {selected.title}!"}
