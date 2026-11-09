"""Searching the catalogue chain (config.yaml drivers.catalog) for titles to add."""

from fastapi import APIRouter, Depends, HTTPException, Query

from penguwatch import library
from penguwatch.api.deps import get_drivers, get_repo
from penguwatch.api.titles import title_item
from penguwatch.db.repository import DatabaseRepository
from penguwatch.security import rate_limit
from penguwatch.sync.active import ActiveDrivers
from penguwatch.textsearch import rank_matches, strip_accents, title_names

router = APIRouter(tags=["Catalog"])

MAX_RESULTS = 40  # what the dialog is given, after ranking
SEARCH_TYPES = {"tv": ("show",), "anime": ("anime",), "movie": ("movie",), "shows": ("show", "anime")}


async def _lookup_ref(catalog, q: str):
    """The single title a pasted link or id (the catalogue's own, TMDB or IMDb) stands for, as a list ([] when the
    catalogue does not know it); None when `q` is not such a link or id."""
    own = getattr(catalog, "parse_link", None)
    ref = (own(q) if own else None) or library.parse_title_ref(q)
    if ref is None:
        return None
    provider, value, media_type = ref
    ids = {provider: value}
    if provider != catalog.key_provider:
        resolved = await catalog.resolve(media_type or "show", ids)
        if not resolved:
            return []
        ids, resolved_type = resolved
        media_type = media_type and resolved_type  # an unknown type (a bare IMDb id) stays unknown: any type is tried
    found = await catalog.title(media_type, ids)
    return [found] if found and found.title else []


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
        results = await _lookup_ref(catalog, q)
        if results is None:
            results = await catalog.search(q, media_types)
            plain = strip_accents(q)
            if not results and plain != q:  # catalogues often only know the plain spelling ("Shogun" for "Shōgun")
                results = await catalog.search(plain, media_types)
            # the drivers rank their own answers; this keeps the order whichever catalogue answered
            results = rank_matches(q, results, names=title_names)[:MAX_RESULTS]
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
