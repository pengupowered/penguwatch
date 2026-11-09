"""Trending titles and recommendations from the discovery chain, and adding discovered titles."""

from penguwatch import clock
from penguwatch.api.schemas import DiscoverAddRequest
import logging
import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import (
    Optional,
    List,
    Dict,
    Any,
)
from fastapi import HTTPException, Query
from penguwatch.models import (
    Show,
    ShowStatus,
    Priority,
    MediaType,
    ShowCreateRequest,
)
from penguwatch.drivers.base import Title
from penguwatch.api.deps import AppContainer, get_container, get_drivers, get_repo, get_sync, household_ids
from penguwatch.db.repository import DatabaseRepository
from penguwatch.optimizer import bundles
from penguwatch.sync.active import ActiveDrivers
from penguwatch.sync.engine import SyncEngine
from penguwatch.sync.titles import rating_fields
from penguwatch.api.titles import _create_title
from fastapi import APIRouter, Depends
from penguwatch.security import rate_limit

logger = logging.getLogger("penguwatch.api.discover")
router = APIRouter(tags=["Discover"])



def catalog_entry(title: Title) -> Dict[str, Any]:
    """A discovered title as the page shows it: its ids, the basics, the built-in ratings, rank and watchers."""
    ratings = rating_fields(title)
    return {
        "ids": dict(title.ids), "media_type": title.media_type, "title": title.title, "year": title.year,
        "poster": title.poster, "overview": title.overview, "network": title.network, "genres": list(title.genres),
        **{column: value for column, value in ratings.items() if not column.endswith("_votes")},
        "rank": title.extra.get("rank"), "watchers": title.extra.get("watchers"),
    }


async def _first_ready(providers):
    for provider in providers:
        if await provider.ready():
            return provider
    return None


async def _annotate_library(items: List[Dict[str, Any]], repo: DatabaseRepository, key_provider: str):
    lib = {s.ids[key_provider]: s for s in await repo.get_shows() if s.ids.get(key_provider)}
    lib_tmdb = {(s.ids["tmdb"], "movie" if s.media_type == MediaType.MOVIE else "show"): s
                for s in lib.values() if s.ids.get("tmdb")}
    all_services = await repo.get_services()
    services = {s.id: s for s in all_services}
    cov = bundles.coverage(all_services)
    for it in items:
        ids = it.get("ids") or {}
        s = lib.get(ids.get(key_provider)) or lib_tmdb.get((ids.get("tmdb"), "movie" if it.get("media_type") == "movie" else "show"))
        it["in_library"] = bool(s)
        it["library_status"] = s.status.value if s else None
        it["library_id"] = s.id if s else None
        if not it.get("service_id"):
            sid = (await repo.match_service_by_network(it.get("network"), title=it.get("title"), services=all_services)
                   if it.get("network") else None)
            it["service_id"] = sid
        svc = services.get(it.get("service_id"))
        it["service_name"] = svc.name if svc else None
        it["service_active"] = bool(svc and bundles.is_effectively_active(svc, cov))


@router.get("/discover/trending", summary="Trending titles")
async def discover_trending(
    media: str = Query("tv"),
    interval: str = Query("week"),
    repo: DatabaseRepository = Depends(get_repo),
    drivers: ActiveDrivers = Depends(get_drivers),
):
    if media not in ("tv", "anime", "movies") or interval not in ("today", "week", "month"):
        raise HTTPException(status_code=400, detail="media must be tv|anime|movies, interval today|week|month")
    discovery = await _first_ready(drivers.discoveries)
    if discovery is None:
        raise HTTPException(status_code=400, detail="No discovery source is set up.")
    cache_key = f"trending:{media}:{interval}"
    cached = await repo.get_state(cache_key)
    items: List[Dict[str, Any]] = []
    if cached:
        c = json.loads(cached)
        if datetime.fromisoformat(c["at"]) > clock.utc_now() - timedelta(hours=3):
            items = c["items"]
    if not items:
        mt = {"tv": "show", "anime": "anime", "movies": "movie"}[media]
        items = [catalog_entry(t) for t in await discovery.trending(mt, interval)]
        if items:
            await repo.set_state(cache_key, json.dumps({"at": clock.utc_now().isoformat(), "items": items}))
    await _annotate_library(items, repo, discovery.key_provider)
    return items


MAX_SEEDS = 15


def recommendation_seeds(shows: List[Show], key: str) -> List[Show]:
    """The titles recommendations start from: rated 8 or more by you, shared by two or more people at high priority,
    or watched / finished with a score of 8.3 or more; your rating first, then the more people want it, then the
    score. With two people, "shared" is "both want it", as before 0.17."""
    seeds = [
        s for s in shows
        if s.ids.get(key) and (
            (s.user_rating or 0) >= 8
            or (len(s.household) >= 2 and s.priority == Priority.HIGH)
            or (s.status in (ShowStatus.COMPLETED, ShowStatus.WATCHING) and (s.imdb_rating or 0) >= 8.3)
        )
    ]
    seeds.sort(key=lambda s: (-(s.user_rating or 0), -len(s.household), -(s.imdb_rating or 0)))
    return seeds[:MAX_SEEDS]


@router.get("/discover/recommendations", summary="Recommendations", dependencies=[Depends(rate_limit("recommendations", 10, 60))])
async def discover_recommendations(
    limit: int = Query(30),
    refresh: bool = Query(False),
    repo: DatabaseRepository = Depends(get_repo),
    drivers: ActiveDrivers = Depends(get_drivers),
):
    """'Because you liked …' picks from the discovery source's recommendations for the household's highest-rated /
    most-wanted titles."""
    discovery = await _first_ready(drivers.discoveries)
    if discovery is None:
        return {"enabled": False, "items": []}
    key = discovery.key_provider
    cached = await repo.get_state("discover.recommendations")
    if cached and not refresh:
        c = json.loads(cached)
        if datetime.fromisoformat(c["at"]) > clock.utc_now() - timedelta(hours=12):
            items = c["items"]
            await _annotate_library(items, repo, key)
            return {"enabled": True, "items": [i for i in items if not i["in_library"]][:limit], "seeds": c.get("seeds", [])}

    shows = await repo.get_shows()
    seeds = recommendation_seeds(shows, key)
    lib_ids = {s.ids[key] for s in shows if s.ids.get(key)}
    sem = asyncio.Semaphore(4)

    async def fetch(seed: Show):
        async with sem:
            try:
                return seed, await discovery.recommendations(seed.media_type.value, seed.ids)
            except Exception as e:
                logger.debug(f"Recommendations for {seed.title} failed: {e}")
                return seed, []

    scores: Dict[Any, Dict[str, Any]] = {}
    for seed, picks in await asyncio.gather(*(fetch(s) for s in seeds)):
        for title, score in picks:
            sid = title.ids.get(key)
            if not sid or sid in lib_ids:
                continue
            entry = scores.setdefault(sid, {**catalog_entry(title), "score": 0.0, "because": []})
            entry["score"] += score
            if seed.title not in entry["because"]:
                entry["because"].append(seed.title)
    ranked = sorted(scores.values(), key=lambda e: e["score"], reverse=True)[:60]
    seed_titles = [s.title for s in seeds]
    await repo.set_state(
        "discover.recommendations", json.dumps({"at": clock.utc_now().isoformat(), "items": ranked, "seeds": seed_titles})
    )
    await _annotate_library(ranked, repo, key)
    return {"enabled": True, "items": [i for i in ranked if not i["in_library"]][:limit], "seeds": seed_titles}


@router.post("/discover/add", summary="Add a discovered title", response_model=Show)
async def discover_add(
    payload: DiscoverAddRequest,
    repo: DatabaseRepository = Depends(get_repo),
    sync: SyncEngine = Depends(get_sync),
    c: AppContainer = Depends(get_container),
):
    """Adds a discovered title by its ids; one the catalogue does not know by its own id (only a TMDB id, say) is
    resolved first."""
    media = payload.media_type
    ids = dict(payload.ids)
    for catalog in sync.catalogs:
        if ids.get(catalog.key_provider):
            break
        try:
            resolved = await catalog.resolve(media, ids)
        except Exception as e:
            logger.debug(f"Resolving a discovered title failed: {e}")
            resolved = None
        if resolved:
            ids, media = resolved
            break
    req = ShowCreateRequest(
        title=payload.title,
        year=payload.year,
        ids=ids,
        media_type=MediaType(media),
        status=payload.status,
        household=household_ids(c.config, payload.household, ["body", "household"]),
        poster=payload.poster,
        overview=payload.overview,
    )
    return await _create_title(req, repo, sync, c.background)
