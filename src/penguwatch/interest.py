"""Likely-interest score (0-100) for a title: quality, taste match and household signals.

Pure module: no I/O, no database, no web framework.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from penguwatch.models import Priority, Show, ShowStatus

PRIOR = 3  # pseudo-titles of neutral affinity added to every genre
NEUTRAL = 0.5
PRIOR_MEAN_RATING = 6.5
PRIOR_VOTES = 500
# Votes assumed for a built-in score that comes without a vote count (the second built-in source)
FALLBACK_VOTES = 500
RATING_FLOOR = 5.0
RATING_SPAN = 4.0
TASTE_REASON_THRESHOLD = 0.65
MAX_REASONS = 4
MAX_TASTE_GENRES = 2

WEIGHT_QUALITY = 0.40
WEIGHT_TASTE = 0.35
WEIGHT_SIGNALS = 0.25

SIGNAL_COMPLETED = 0.7
SIGNAL_WATCHING = 0.6
SIGNAL_DROPPED = 0.15

BONUS_HIGH_PRIORITY = 0.5
BONUS_MEDIUM_PRIORITY = 0.2
BONUS_BOTH = 0.5  # per person beyond the first who wants the title (with two people: when both do)


@dataclass(frozen=True)
class TasteProfile:
    genre_sum: Dict[str, float]  # sum of signals per genre
    genre_count: Dict[str, int]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _signal(show: Show) -> Optional[float]:
    """How much the household liked this title (0..1), or None when there is no evidence."""
    # Seam for later: custom rating sources (title_ratings, normalised by each source's scale) could be weighed in here.
    if show.user_rating is not None:
        return show.user_rating / 10.0
    if show.status == ShowStatus.COMPLETED:
        return SIGNAL_COMPLETED
    if show.status == ShowStatus.WATCHING and show.watched_episodes > 0:
        return SIGNAL_WATCHING
    if show.status == ShowStatus.DROPPED:
        return SIGNAL_DROPPED
    return None


def _unique_genres(show: Show) -> List[str]:
    return list(dict.fromkeys(show.genres))


def build_profile(shows: Iterable[Show]) -> TasteProfile:
    genre_sum: Dict[str, float] = {}
    genre_count: Dict[str, int] = {}
    for show in shows:
        s = _signal(show)
        if s is None:
            continue
        for g in _unique_genres(show):
            genre_sum[g] = genre_sum.get(g, 0.0) + s
            genre_count[g] = genre_count.get(g, 0) + 1
    return TasteProfile(genre_sum=genre_sum, genre_count=genre_count)


def _affinities(show: Show, profile: TasteProfile) -> Dict[str, float]:
    """A_g per genre of the title, with the title's own contribution removed (leave-one-out)."""
    own = _signal(show)
    result: Dict[str, float] = {}
    for g in _unique_genres(show):
        total = profile.genre_sum.get(g, 0.0)
        count = profile.genre_count.get(g, 0)
        if own is not None:
            total -= own
            count = max(0, count - 1)
        result[g] = (total + PRIOR * NEUTRAL) / (count + PRIOR)
    return result


def _quality(show: Show) -> Tuple[float, bool]:
    """(Q in 0..1, has_rating); votes shrink the rating toward the prior mean."""
    if show.imdb_rating:
        r, n = show.imdb_rating, show.imdb_votes or 0
    elif show.simkl_rating:
        r, n = show.simkl_rating, FALLBACK_VOTES
    else:
        return NEUTRAL, False
    r_adj = (r * n + PRIOR_MEAN_RATING * PRIOR_VOTES) / (n + PRIOR_VOTES)
    return _clamp((r_adj - RATING_FLOOR) / RATING_SPAN), True


def _signals(show: Show) -> float:
    h = 0.0
    if show.priority == Priority.HIGH:
        h += BONUS_HIGH_PRIORITY
    elif show.priority == Priority.MEDIUM:
        h += BONUS_MEDIUM_PRIORITY
    h += BONUS_BOTH * max(len(show.household) - 1, 0)
    return _clamp(h)


def _format_votes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _label_of(column: str) -> str:
    """The label of the built-in rating source stored in `column`."""
    from penguwatch.ratings import BUILTIN_SOURCES, BUILTIN_COLUMNS
    source = next((sid for sid, col in BUILTIN_COLUMNS.items() if col == column), None)
    return next((s.label for s in BUILTIN_SOURCES if s.id == source), column)


def _quality_reason(show: Show) -> Optional[str]:
    if show.imdb_rating:
        if show.imdb_votes:
            return f"IMDb {show.imdb_rating:.1f} ({_format_votes(show.imdb_votes)} votes)"
        return f"IMDb {show.imdb_rating:.1f}"
    if show.simkl_rating:
        return f"{_label_of('simkl_rating')} {show.simkl_rating:.1f}"
    return None


def _taste_reason(affinity: Dict[str, float]) -> Optional[str]:
    liked = sorted(((a, g) for g, a in affinity.items() if a >= TASTE_REASON_THRESHOLD), key=lambda t: (-t[0], t[1]))
    if not liked:
        return None
    return "You usually finish: " + ", ".join(g for _, g in liked[:MAX_TASTE_GENRES])


def _shared_reason(show: Show, household_size: int) -> Optional[str]:
    """"Both want it" (two people), "Everyone wants it" (all of three or more), "Shared by k" (k of them, k >= 2)."""
    k = len(show.household)
    if k < 2:
        return None
    if household_size <= 2:
        return "Both want it"
    return "Everyone wants it" if k >= household_size else f"Shared by {k}"


def score(show: Show, profile: TasteProfile, household_size: int = 2) -> Tuple[int, List[str]]:
    """0-100 and up to 4 short reason strings. `household_size`: how many people the household has (the words of the
    "shared" reason)."""
    affinity = _affinities(show, profile)
    taste = sum(affinity.values()) / len(affinity) if affinity else NEUTRAL
    quality, rated = _quality(show)
    value = round(100 * (WEIGHT_QUALITY * quality + WEIGHT_TASTE * taste + WEIGHT_SIGNALS * _signals(show)))
    reasons: List[str] = []
    for text in (
        _quality_reason(show),
        _taste_reason(affinity),
        _shared_reason(show, household_size),
        "High priority" if show.priority == Priority.HIGH else None,
        None if rated else "Little rating data",
    ):
        if text:
            reasons.append(text)
    return int(value), reasons[:MAX_REASONS]
