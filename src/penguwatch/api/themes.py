"""GET /api/themes: the themes the page can show and the server's default (config.yaml display.theme, docs/themes.md).
The page starts with a short list of its own (<html data-looks>); this one adds the descriptions."""

from typing import List

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from penguwatch.api.deps import get_config
from penguwatch.config import AppConfig
from penguwatch.web import themes

router = APIRouter(tags=["Themes"])


class ThemeInfo(BaseModel):
    id: str
    name: str
    description: str
    modes: List[str]
    tags: List[str]
    css: str = ""  # the generated stylesheet the page links while the theme is in use; none for `default`


class ThemeList(BaseModel):
    default: str
    themes: List[ThemeInfo]


@router.get("/themes", response_model=ThemeList, summary="List the themes")
async def get_themes(request: Request, config: AppConfig = Depends(get_config)) -> ThemeList:
    """Every theme, `default` first, and `default`: the theme a browser shows until its person picks one (display.theme,
    or `default` when the configured id is not a theme)."""
    return ThemeList(default=themes.resolve_default(config.display.theme),
                     themes=[ThemeInfo(**t) for t in themes.api_list(request.app.state.asset_version)])
