"""Rating sources and the ratings users bring: a source list, a batch import, and per-title values."""

from typing import Annotated, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel

from penguwatch.api.deps import _show_or_404, get_repo, unprocessable
from penguwatch.db.ratings import InvalidScale, RatingOutOfScale, UnknownRatingSource
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import Show
from penguwatch.ratings import (
    builtin_rating_columns,
    SOURCE_ID_PATTERN, RatingImportRequest, RatingMatch, RatingSource, RatingSourceSpec,
    RatingSourcePatch, RatingValue, scale_error,
)
from penguwatch.security import rate_limit

router = APIRouter(tags=["Ratings"])

SourceId = Annotated[str, Path(pattern=SOURCE_ID_PATTERN, description="Rating source id (a lowercase slug)")]


class Candidate(BaseModel):
    id: int
    title: str
    year: Optional[int] = None
    media_type: str = "show"


class UnmatchedItem(BaseModel):
    index: int
    match: RatingMatch


class AmbiguousItem(BaseModel):
    index: int
    match: RatingMatch
    candidates: List[Candidate]


class ImportResult(BaseModel):
    updated: int
    unchanged: int
    unmatched: List[UnmatchedItem]
    ambiguous: List[AmbiguousItem]


class ShowRating(BaseModel):
    """One source on one title: the source's settings, and the value when the title has one."""

    source: str
    name: str
    label: str
    scale_min: float
    scale_max: float
    decimals: int
    builtin: bool
    show_on_cards: bool
    value: Optional[float] = None
    votes: Optional[int] = None
    url: Optional[str] = None
    updated_at: Optional[str] = None


async def _custom_source(repo: DatabaseRepository, source_id: str) -> RatingSource:
    """The source an import or edit targets: it must exist and be a custom one."""
    source = await repo.get_rating_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Rating source not found")
    if source.builtin:
        raise HTTPException(status_code=409, detail=f"{source.name} is a built-in rating and cannot be changed here")
    return source


async def _show_ratings(repo: DatabaseRepository, show: Show) -> List[ShowRating]:
    stored = await repo.get_title_ratings(show.id)
    builtin_values = {source: (getattr(show, column), getattr(show, votes) if votes else None)
                      for source, (column, votes) in builtin_rating_columns().items()}
    out = []
    for s in await repo.get_rating_sources():
        if s.builtin and s.id in builtin_values:
            value, votes = builtin_values[s.id]
            entry = dict(value=value, votes=votes)
        else:
            t = stored.get(s.id)
            entry = dict(value=t.value, votes=t.votes, url=t.url, updated_at=t.updated_at) if t else {}
        out.append(ShowRating(source=s.id, name=s.name, label=s.label, scale_min=s.scale_min, scale_max=s.scale_max,
                              decimals=s.decimals, builtin=s.builtin, show_on_cards=s.show_on_cards, **entry))
    return out


# --- sources ---

@router.get("/rating-sources", response_model=List[RatingSource], summary="List rating sources")
async def list_rating_sources(repo: DatabaseRepository = Depends(get_repo)):
    """The built-in sources (the drivers') and the custom ones, with how many titles each rates."""
    return await repo.get_rating_sources()


@router.post("/rating-sources", response_model=RatingSource, status_code=201, summary="Create a rating source")
async def create_rating_source(spec: RatingSourceSpec, repo: DatabaseRepository = Depends(get_repo)):
    try:
        return await repo.create_rating_source(spec)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.patch("/rating-sources/{source}", response_model=RatingSource, summary="Edit a rating source")
async def update_rating_source(source: SourceId, patch: RatingSourcePatch, repo: DatabaseRepository = Depends(get_repo)):
    """Name, label, scale, decimals, whether it shows on cards, and its position. Sources defined in config.yaml
    are changed there."""
    current = await _custom_source(repo, source)
    if current.read_only:
        raise HTTPException(status_code=409, detail="This source is defined in config.yaml; change it there")
    try:
        updated = await repo.update_rating_source(source, patch.model_dump(exclude_none=True))
    except InvalidScale as e:
        raise unprocessable(["body", "scale_min"], str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return updated


@router.delete("/rating-sources/{source}", summary="Delete a rating source and its values")
async def delete_rating_source(source: SourceId, repo: DatabaseRepository = Depends(get_repo)):
    current = await _custom_source(repo, source)
    if current.read_only:
        raise HTTPException(status_code=409, detail="This source is defined in config.yaml; remove it there")
    await repo.delete_rating_source(source)
    return {"status": "deleted", "id": source, "removed_values": current.rated_titles}


# --- values ---

@router.put("/ratings/{source}", response_model=ImportResult, summary="Import ratings in bulk",
            dependencies=[Depends(rate_limit("ratings-import", 60, 60))])
async def import_ratings(source: SourceId, payload: RatingImportRequest, repo: DatabaseRepository = Depends(get_repo)):
    """Sets the source's value on up to 1000 titles. Each item names its title by one external id (`imdb`, `tmdb`,
    `tvdb`, or any provider a title's `ids` has) or by `title` with `year`. A value outside the source's scale refuses the whole batch with 422
    and nothing is written. Items that match no title, or several, are listed in the answer and skipped. Sending
    an item again replaces its value, votes and link."""
    src = await _custom_source(repo, source)
    errors = []
    for i, item in enumerate(payload.items):
        message = scale_error(src, item.value)
        if message:
            errors.append({"type": "value_error", "loc": ["body", "items", i, "value"], "msg": message})
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    index = await repo.title_index()
    resolved: Dict[int, RatingValue] = {}
    unmatched, ambiguous = [], []
    for i, item in enumerate(payload.items):
        found = index.find(item.match)
        if not found:
            unmatched.append(UnmatchedItem(index=i, match=item.match))
        elif len(found) > 1:
            ambiguous.append(AmbiguousItem(index=i, match=item.match, candidates=[
                Candidate(id=r["id"], title=r["title"], year=r["year"], media_type=r["media_type"] or "show")
                for r in found[:10]]))
        else:
            resolved[found[0]["id"]] = RatingValue(value=item.value, votes=item.votes, url=item.url)  # the last one wins
    try:
        changed, unchanged = await repo.set_title_ratings(source, resolved, events="summary")
    except UnknownRatingSource:
        raise HTTPException(status_code=404, detail="Rating source not found")  # deleted while this ran
    except RatingOutOfScale as e:  # the source's scale changed while this ran
        raise unprocessable(["body", "items"], str(e))
    return ImportResult(updated=len(changed), unchanged=unchanged, unmatched=unmatched, ambiguous=ambiguous)


@router.delete("/ratings/{source}", summary="Clear every value of a rating source")
async def clear_ratings(source: SourceId, repo: DatabaseRepository = Depends(get_repo)):
    """Removes the values, keeps the source."""
    await _custom_source(repo, source)
    return {"status": "cleared", "source": source, "removed": await repo.clear_source_ratings(source)}


@router.delete("/ratings/{source}/{show_id}", status_code=204, summary="Remove one title's value")
async def delete_title_rating(source: SourceId, show_id: int, repo: DatabaseRepository = Depends(get_repo)):
    await _custom_source(repo, source)
    if not await repo.delete_title_rating(source, show_id):
        raise HTTPException(status_code=404, detail="That title has no value from this source")
    return Response(status_code=204)


@router.get("/shows/{show_id}/ratings", response_model=List[ShowRating], summary="Every rating source on a title")
async def get_show_ratings(show_id: int, repo: DatabaseRepository = Depends(get_repo)):
    """All sources, built-in and custom, with this title's value for each (null when it has none)."""
    return await _show_ratings(repo, await _show_or_404(repo, show_id))


@router.put("/shows/{show_id}/ratings/{source}", response_model=List[ShowRating], summary="Set one title's value")
async def set_show_rating(show_id: int, source: SourceId, payload: RatingValue, repo: DatabaseRepository = Depends(get_repo)):
    show = await _show_or_404(repo, show_id)
    src = await _custom_source(repo, source)
    message = scale_error(src, payload.value)
    if message:
        raise unprocessable(["body", "value"], message)
    try:
        await repo.set_title_ratings(source, {show_id: payload})
    except UnknownRatingSource:
        raise HTTPException(status_code=404, detail="Rating source not found")
    except RatingOutOfScale as e:
        raise unprocessable(["body", "value"], str(e))
    return await _show_ratings(repo, show)
