"""Searching the catalogue chain (config.yaml drivers.catalog) for titles to add."""

from fastapi import APIRouter, Depends, HTTPException, Query

from penguwatch.api.deps import get_drivers, get_repo
from penguwatch.api.titles import title_item
from penguwatch.db.repository import DatabaseRepository
from penguwatch.security import rate_limit
from penguwatch.sync.active import ActiveDrivers
from penguwatch.textsearch import strip_accents

router = APIRouter(tags=["Catalog"])

SEARCH_TYPES = {"tv": ("show",), "anime": ("anime",), "movie": ("movie",), "shows": ("show", "anime")}


@router.get("/catalog/search", summary="Search the catalogue", dependencies=[Depends(rate_limit("catalog-search", 30, 60))])
async def search_catalog(
    q: str = Query(..., min_length=1),
    type: str = Query("all", description="tv, anime, movie, shows (tv and anime) or all"),
    repo: DatabaseRepository = Depends(get_repo),
    drivers: ActiveDrivers = Depends(get_drivers),
):
    """Searches the first ready catalogue (TV, anime, movies) for titles to add. Each result has the title's `ids`."""
    media_types = SEARCH_TYPES.get(type, ("show", "anime", "movie"))
    catalog = None
    for candidate in drivers.catalogs:
        if await candidate.ready():
            catalog = candidate
            break
    if catalog is None:
        raise HTTPException(status_code=400, detail="No catalogue is set up for search.")
    try:
        results = await catalog.search(q, media_types)
        plain = strip_accents(q)
        if not results and plain != q:  # catalogues often only know the plain spelling ("Shogun" for "Shōgun")
            results = await catalog.search(plain, media_types)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")
    all_services = await repo.get_services()
    services = {s.id: s.name for s in all_services}
    lib = await repo.get_external_ids(catalog.key_provider)
    out = []
    for title in results:
        item = title_item(title)
        s_id = await repo.match_service_by_network(title.network, title=title.title, services=all_services)
        if not s_id and title.media_type == "anime":
            s_id = await repo.default_anime_service()
        item["service_id"] = s_id
        item["service_name"] = services.get(s_id, s_id.replace("_", " ").title() if s_id else "Unknown")
        item["in_library"] = title.ids.get(catalog.key_provider) in lib
        out.append(item)
    return out
