"""Adding a title to the library: look it up in the catalogue, apply the library rules, store it, queue the sync.

The decisions live in `penguwatch.library` (pure functions); this module only does the I/O around them, in small
steps so each can be read (and tested) on its own.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from penguwatch import library
from penguwatch.api.deps import _spawn
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.base import Title
from penguwatch.models import MediaType, Show, ShowCreateRequest
from penguwatch.sync.engine import SyncEngine
from penguwatch.textsearch import clean_en_title

logger = logging.getLogger("penguwatch.api.titles")

Match = Tuple[Dict[str, Any], Optional[MediaType], Optional[Title]]


def title_item(title: Title) -> Dict[str, Any]:
    """A catalogue title as the API shows a search result."""
    item: Dict[str, Any] = {
        "ids": dict(title.ids), "media_type": title.media_type, "title": title.title, "year": title.year,
        "poster": title.poster, "overview": title.overview, "network": title.network, "genres": list(title.genres),
        "status": title.status, "total_episodes": title.total_episodes, "runtime": title.runtime,
        "ratings": {source: {"rating": r.value, "votes": r.votes} for source, r in title.ratings.items()
                    if not r.fallback},
    }
    en_title = clean_en_title(title.title, title.en_title)
    if en_title:
        item["en_title"] = en_title
    return item


async def _catalog(sync: SyncEngine, ids: Dict[str, Any]):
    """The catalogue to look the title up in: the first ready one that has the title's id, else the first ready."""
    ready = [c for c in sync.catalogs if await c.ready()]
    return next((c for c in ready if ids.get(c.key_provider)), ready[0] if ready else None)


async def _search_catalogue(payload: ShowCreateRequest, catalog) -> Match:
    """Finds the catalogue's title for a name. (the request's ids, <requested type>, None) when nothing matches."""
    if catalog is None or payload.ids.get(catalog.key_provider) or not payload.title:
        return dict(payload.ids), payload.media_type, None
    try:
        matches = await catalog.search(payload.title, library.search_media_types(payload.media_type), limit=5,
                                       detailed=1)
        first = library.pick_search_match(matches, payload.year)
        if first:
            return {**payload.ids, **first.ids}, MediaType(first.media_type), first
    except Exception as e:
        logger.debug(f"Catalogue title search error: {e}")
    return dict(payload.ids), payload.media_type, None


async def _fetch_details(catalog, ids: Dict[str, Any], media_type: Optional[MediaType]) -> Tuple[Optional[Title], Optional[MediaType]]:
    """The catalogue's details for a known id; each media type is tried when the type isn't known."""
    try:
        found = await catalog.title(media_type.value if media_type else None, ids)
    except Exception:
        found = None
    if found and found.title:
        return found, MediaType(found.media_type)
    return None, media_type


async def _resolve_service(repo: DatabaseRepository, payload: ShowCreateRequest, merged: library.Merged, media_type: MediaType) -> Tuple[Optional[str], Optional[str]]:
    """(service_id, network): explicit choice, else matched by network name, else the anime default."""
    service_id = payload.service_id
    network = merged.network
    if not service_id:
        service_id = await repo.match_service_by_network(network, title=merged.title)
    if media_type == MediaType.ANIME and not service_id:
        service_id = await repo.default_anime_service()
        if service_id:
            network = network or "Crunchyroll"
    return service_id, network


async def _create_title(
    payload: ShowCreateRequest,
    repo: DatabaseRepository,
    sync: SyncEngine,
    background: set,
) -> Show:
    existing = await repo.find_show_by_ids(payload.ids)
    if existing:
        return existing

    catalog = await _catalog(sync, payload.ids)
    ids, media_type, details = await _search_catalogue(payload, catalog)
    key = ids.get(catalog.key_provider) if catalog else None

    if key:
        existing = await repo.find_show_by_ids(ids)
        if existing:
            return existing
        if not details:
            details, media_type = await _fetch_details(catalog, ids, media_type)

    media_type = media_type or MediaType.SHOW
    merged = library.merge_details(payload, details, media_type)
    service_id, network = await _resolve_service(repo, payload, merged, media_type)
    show = library.assemble_show(
        payload, merged, ids={catalog.key_provider: key} if key else {}, media_type=media_type,
        service_id=service_id, network=network,
    )
    show.ids = {**ids, **show.ids, **sync.new_title_ids()}

    show_id = await repo.upsert_show(show)
    created = await repo.get_show_by_id(show_id)
    await sync.store_ratings(created, details)  # built-in sources without a column (title_ratings)

    # Two-way sync: put it on the library's list too, then fetch episodes in the background
    # (also while the account is not connected: the worker delivers it after the login, and until then a pull
    # keeps the title)
    if sync.library.remote and created.ids.get(sync.library.key_provider):
        await sync.queue_list_move(created, created.status, new_title=True)
    if sync.catalog_for(created):
        _spawn(background, _post_create_refresh(created.id, repo, sync))
    return created


async def _post_create_refresh(show_id: int, repo: DatabaseRepository, sync: SyncEngine):
    try:
        show = await repo.get_show_by_id(show_id)
        if not show:
            return
        await sync.ensure_episode_list(show, deadline=None)  # nobody is waiting for this one
        if sync.library.is_connected():
            await sync.flush_outbox()
    except Exception as e:
        logger.debug(f"Post-create refresh failed for {show_id}: {e}")
