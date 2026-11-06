"""Library endpoints: list/add/update/delete titles and progress tracking."""

from datetime import timedelta
from enum import Enum
from typing import (
    Optional,
    List,
    Dict,
    Any,
)
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from penguwatch import clock
from penguwatch.interest import score
from penguwatch.models import (
    Show,
    ShowStatus,
    Priority,
    HouseholdTag,
    MediaType,
    ShowCreateRequest,
    ShowUpdateRequest,
    ShowProgressUpdate,
    EpisodeToggleRequest,
    SeasonToggleRequest,
)
from penguwatch.api.deps import AppContainer, _show_or_404, unprocessable, get_container, get_repo, get_sync
from penguwatch.db.ratings import UnknownRatingSource
from penguwatch.db.repository import DatabaseRepository
from penguwatch.ratings import parse_min_rating
from penguwatch.sync.engine import SyncEngine
from penguwatch.api.titles import _create_title
from fastapi import APIRouter

router = APIRouter(tags=["Library"])

# Fields the library cards, filters and the interest badge read (web/static/js/library.js, util.js ratingBadges /
# statusOptionsFor). `GET /api/shows?view=card` returns only these (plus interest_score and
# interest_reasons when sorting by interest); the default full shape is unchanged. The overview, banner,
# timestamps, sequels and the next-episode fields are left out: they made up most of the payload.
CARD_FIELDS = frozenset({
    "id", "ids", "media_type", "title", "en_title", "year", "poster", "network", "service_id", "service_manual",
    "status", "priority", "household_tag", "simkl_rating", "imdb_rating",
    "user_rating", "total_episodes", "watched_episodes", "aired_episodes", "next_to_watch", "last_watched",
    "ratings", "interest_score", "interest_reasons", "episodes_left", "is_caught_up", "is_ready_to_binge",
})


# The card fields in the order model_dump() gives them: the model's fields, then its computed fields.
_CARD_PLAIN = tuple(name for name in Show.model_fields if name in CARD_FIELDS)
_CARD_COMPUTED = tuple(name for name in Show.model_computed_fields if name in CARD_FIELDS)


def _card(show: Show, on_cards: set) -> Dict[str, Any]:
    """`show.model_dump(mode="json", include=CARD_FIELDS)` with the ratings narrowed to the sources shown on cards,
    without running the model serializer for every title (tests/test_shows_cache.py compares the two)."""
    card: Dict[str, Any] = {}
    for name in _CARD_PLAIN:
        if name == "ratings":
            card[name] = {k: r.model_dump(mode="json") for k, r in show.ratings.items() if k in on_cards}
            continue
        if name in ("interest_score", "interest_reasons") and show.interest_score is None:
            continue  # only present while a score was computed (see Show._omit_unset_interest)
        value = getattr(show, name)
        card[name] = value.value if isinstance(value, Enum) else value
    for name in _CARD_COMPUTED:
        card[name] = getattr(show, name)
    return card


@router.get("/shows", summary="List library titles", response_model=List[Show])
async def list_shows(
    service_id: Optional[str] = Query(None),
    only_active_services: bool = Query(False),
    status: Optional[ShowStatus] = Query(None),
    priority: Optional[Priority] = Query(None),
    household_tag: Optional[HouseholdTag] = Query(None),
    media_type: Optional[MediaType] = Query(None),
    search: Optional[str] = Query(None),
    sort_by: str = Query("priority", description="priority, a built-in rating source's id (imdb, ...), my_rating, "
                         "title, next_air, recent, interest, or rating:<source id> for any rating source"),
    min_rating: List[str] = Query([], description="<source id>:<lowest value>, e.g. letterboxd:3.5; repeat to combine"),
    view: Optional[str] = Query(None, pattern="^card$", description="card = only the fields the library cards use"),
    repo: DatabaseRepository = Depends(get_repo),
):
    try:
        minimums = [parse_min_rating(m) for m in min_rating]
    except ValueError as e:
        raise unprocessable(["query", "min_rating"], str(e))
    try:
        shows = await repo.get_shows(
            service_id=service_id,
            only_active_services=only_active_services,
            status=status,
            priority=priority,
            household_tag=household_tag,
            search_query=search,
            sort_by=sort_by,
            media_type=media_type,
            min_ratings=minimums,
        )
    except UnknownRatingSource as e:
        raise unprocessable(["query", "sort_by" if sort_by.startswith("rating:") else "min_rating"], str(e))
    if sort_by == "interest":
        shows = await _rank_by_interest(repo, shows)
    if view == "card":
        on_cards = {s.id for s in await repo.get_rating_sources() if s.show_on_cards and not s.builtin}
        # only the sources marked "show on cards"; the full shape carries them all
        return JSONResponse([_card(s, on_cards) for s in shows])
    return shows


async def _rank_by_interest(repo: DatabaseRepository, shows: List[Show]) -> List[Show]:
    """Attach the likely-interest score (profile built from the whole library) and order by it."""
    profile = await repo.taste_profile()
    for show in shows:
        show.interest_score, show.interest_reasons = score(show, profile)
    return sorted(shows, key=lambda s: (-(s.interest_score or 0), -(s.imdb_rating or 0.0), s.title))


@router.get("/shows/{show_id}", summary="Get one title", response_model=Show)
async def get_show(show_id: int, repo: DatabaseRepository = Depends(get_repo)):
    return await _show_or_404(repo, show_id)



@router.post("/shows", summary="Add a title", response_model=Show)
async def create_show(
    payload: ShowCreateRequest,
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
    c: AppContainer = Depends(get_container),
):
    """Adds a show/movie (from a catalogue search or by hand) and puts it on the library's list."""
    return await _create_title(payload, repo, sync, c.background)


@router.patch("/shows/{show_id}", summary="Update a title", response_model=Show)
async def update_show(
    show_id: int,
    payload: ShowUpdateRequest,
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
):
    show = await _show_or_404(repo, show_id)
    if payload.status and payload.status != show.status:
        await sync.update_show_status(show_id, payload.status)
    if payload.clear_user_rating:
        await sync.update_show_rating(show_id, None)
    elif payload.user_rating is not None and payload.user_rating != show.user_rating:
        await sync.update_show_rating(show_id, payload.user_rating)
    if payload.watched_episodes is not None:
        await sync.set_watched_count(show_id, payload.watched_episodes)

    updated = await repo.update_show_metadata(
        show_id=show_id,
        priority=payload.priority,
        household_tag=payload.household_tag,
        service_id=payload.service_id,
        network=payload.network,
        notes=payload.notes,
    )
    return updated


@router.post("/shows/{show_id}/increment", summary="Mark the next episode watched", response_model=Show)
async def increment_episode(
    show_id: int, sync: SyncEngine = Depends(get_sync), repo: DatabaseRepository = Depends(get_repo)
):
    """Marks the next episode watched (the exact season/episode is pushed to the library)."""
    await _show_or_404(repo, show_id)
    return await sync.mark_next_episode(show_id)


@router.post("/shows/{show_id}/decrement", summary="Unmark the last watched episode", response_model=Show)
async def decrement_episode(
    show_id: int, sync: SyncEngine = Depends(get_sync), repo: DatabaseRepository = Depends(get_repo)
):
    await _show_or_404(repo, show_id)
    return await sync.unmark_last_episode(show_id)


@router.post("/shows/{show_id}/progress", summary="Set watch progress", response_model=Show)
async def update_show_progress_endpoint(
    show_id: int,
    payload: ShowProgressUpdate,
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
):
    """Sets progress by absolute count, +/- episodes, or +/- whole seasons."""
    await _show_or_404(repo, show_id)
    if payload.watched_episodes is not None:
        return await sync.set_watched_count(show_id, payload.watched_episodes)
    if payload.episode_delta:
        updated = None
        for _ in range(abs(payload.episode_delta)):
            updated = await (sync.mark_next_episode(show_id) if payload.episode_delta > 0 else sync.unmark_last_episode(show_id))
        return updated
    if payload.season_delta:
        return await sync.step_season(show_id, payload.season_delta)
    return await repo.get_show_by_id(show_id)


@router.get("/shows/{show_id}/episodes", summary="Episode checklist by season")
async def list_episodes(
    show_id: int,
    refresh: bool = Query(False),
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
):
    """Season-by-season episode checklist."""
    show = await _show_or_404(repo, show_id)
    if sync.catalog_for(show) and show.media_type != MediaType.MOVIE:
        await sync.ensure_episode_list(show, max_age=timedelta(minutes=5) if refresh else timedelta(hours=12))
        show = await repo.get_show_by_id(show_id)
    today = clock.today().isoformat()
    seasons: Dict[int, Dict[str, Any]] = {}
    for e in await repo.get_episodes(show_id):
        if e.season <= 0:
            continue
        s = seasons.setdefault(e.season, {"number": e.season, "episodes": [], "watched": 0, "aired": 0})
        aired = bool(e.air_date and e.air_date[:10] <= today) or e.is_watched
        s["episodes"].append({**e.model_dump(), "aired": aired})
        s["aired"] += 1 if aired else 0
        s["watched"] += 1 if e.is_watched else 0
    return {
        "show": show,
        "complete_list": bool(show.episodes_synced_at),
        "seasons": [seasons[k] for k in sorted(seasons)],
    }


@router.post("/shows/{show_id}/episodes/toggle", summary="Mark one episode watched or unwatched", response_model=Show)
async def toggle_episode(
    show_id: int,
    payload: EpisodeToggleRequest,
    sync: SyncEngine = Depends(get_sync),
    repo: DatabaseRepository = Depends(get_repo),
):
    await _show_or_404(repo, show_id)
    return await sync.set_episodes_watched(show_id, [(payload.season, payload.number)], payload.watched)


@router.post("/shows/{show_id}/seasons/toggle", summary="Mark a season watched or unwatched", response_model=Show)
async def toggle_season(
    show_id: int,
    payload: SeasonToggleRequest,
    sync: SyncEngine = Depends(get_sync),
    repo: DatabaseRepository = Depends(get_repo),
):
    await _show_or_404(repo, show_id)
    return await sync.set_season_watched(show_id, payload.season, payload.watched)


@router.delete("/shows/{show_id}", summary="Delete a title")
async def delete_show(
    show_id: int,
    remove_from_library: bool = Query(True, description="also remove it from the library driver's account"),
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
):
    show = await _show_or_404(repo, show_id)
    await repo.delete_show(show_id)
    queued = False
    if remove_from_library:  # the engine decides: no id of the account's, or no account at all, queues nothing
        queued = await sync.remove_from_library(show)
    return {"status": "deleted", "id": show_id, "removed_from_library": queued}
