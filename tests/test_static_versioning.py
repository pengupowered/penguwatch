"""Hash-prefixed static serving: /static/v/<hash>/... is immutable, unversioned module files revalidate."""

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import AsyncClient, ASGITransport

from penguwatch.main import ASSET_VERSION, STATIC_DIR, _asset_version, add_versioned_static, app, security_config
from penguwatch.security import SecurityConfig, SecurityMiddleware


def client(target=None):
    return AsyncClient(transport=ASGITransport(app=target or app), base_url="http://test")


_GUARD = SecurityMiddleware(None, security_config)  # only its header rules are used here


def _cache_control(path: str) -> str:
    headers = dict(_GUARD._security_headers(path))
    return headers.get(b"cache-control", b"").decode()


# --- header matrix ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/api/shows", "no-store"),
    ("/static/v/abc1234567/js/probe.js", "public, max-age=31536000, immutable"),
    ("/static/js/probe.js", "no-cache"),
    ("/static/theme.js", "public, max-age=86400"),
    ("/", ""),
])
def test_cache_header_matrix(path, expected):
    assert _cache_control(path) == expected


# --- real app --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_versioned_asset_is_served_immutable():
    async with client() as ac:
        plain = await ac.get("/static/theme.js")
        versioned = await ac.get(f"/static/v/{ASSET_VERSION}/theme.js")
        head = await ac.head(f"/static/v/{ASSET_VERSION}/style.css")
    assert versioned.status_code == 200
    assert versioned.content == plain.content
    assert "immutable" in versioned.headers["cache-control"]
    assert "max-age=31536000" in versioned.headers["cache-control"]
    assert head.status_code == 200


@pytest.mark.asyncio
async def test_unversioned_asset_keeps_day_cache():
    async with client() as ac:
        res = await ac.get("/static/theme.js")
    assert res.status_code == 200
    assert "max-age=86400" in res.headers["cache-control"]
    assert "immutable" not in res.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/static/v/0000000000/theme.js",
    f"/static/v/{ASSET_VERSION}/nope.js",
    f"/static/v/{ASSET_VERSION}/..%2Fmain.py",
    f"/static/v/{ASSET_VERSION}/%2e%2e/main.py",
    f"/static/v/{ASSET_VERSION}/",
])
async def test_versioned_path_rejects_wrong_hash_missing_and_traversal(path):
    async with client() as ac:
        res = await ac.get(path)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_versioned_asset_is_public_when_a_token_is_configured(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    async with client() as ac:
        res = await ac.get(f"/static/v/{ASSET_VERSION}/theme.js")
    assert res.status_code == 200


def test_versioned_route_is_not_in_openapi():
    assert not any(p.startswith("/static") for p in app.openapi()["paths"])


# --- reusable pieces against a throwaway directory ---------------------------------------------

def test_asset_version_covers_subdirectories(tmp_path):
    (tmp_path / "a.js").write_text("a")
    before = _asset_version(tmp_path)
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "probe.js").write_text("x")
    after = _asset_version(tmp_path)
    assert before != after
    (tmp_path / "js" / "probe.js").write_text("y")
    assert _asset_version(tmp_path) != after


def test_asset_version_of_shipped_tree_is_stable():
    assert _asset_version(STATIC_DIR) == ASSET_VERSION


@pytest.mark.asyncio
async def test_versioned_module_directory_on_throwaway_app(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "probe.js").write_text("export const probe = 1;\n")
    (tmp_path / "top.js").write_text("// top\n")
    version = _asset_version(tmp_path)
    sub = FastAPI()
    add_versioned_static(sub, tmp_path, version)
    sub.mount("/static", StaticFiles(directory=str(tmp_path)), name="static")
    wrapped = SecurityMiddleware(sub, cfg=SecurityConfig())
    async with client(wrapped) as ac:
        ok = await ac.get(f"/static/v/{version}/js/probe.js")
        other = await ac.get("/static/v/deadbeef00/js/probe.js")
        plain = await ac.get("/static/js/probe.js")
        top = await ac.get("/static/top.js")
    assert ok.status_code == 200 and "probe" in ok.text
    assert "immutable" in ok.headers["cache-control"]
    assert other.status_code == 404
    assert plain.status_code == 200 and plain.headers["cache-control"] == "no-cache"
    assert top.headers["cache-control"] == "public, max-age=86400"


# --- errors under the versioned prefix are never cached ----------------------------------------

def test_versioned_errors_are_no_store_unit():
    assert dict(_GUARD._security_headers("/static/v/x/a.js", 404))[b"cache-control"] == b"no-store"
    ok = dict(_GUARD._security_headers("/static/v/x/a.js", 200))[b"cache-control"]
    assert ok == b"public, max-age=31536000, immutable"
    assert dict(_GUARD._security_headers("/static/v/x/a.js", 304))[b"cache-control"] == ok


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/static/v/0000000000/theme.js", f"/static/v/{ASSET_VERSION}/nope.js"])
async def test_versioned_404_is_no_store(path):
    async with client() as ac:
        res = await ac.get(path)
    assert res.status_code == 404
    assert res.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_index_loads_the_app_as_an_es_module():
    async with client() as ac:
        page = await ac.get("/")
        main = await ac.get(f"/static/v/{ASSET_VERSION}/js/main.js")
        module = await ac.get(f"/static/v/{ASSET_VERSION}/js/tabs.js")
    assert f'<script type="module" src="/static/v/{ASSET_VERSION}/js/main.js"></script>' in page.text
    for res in (main, module):
        assert res.status_code == 200
        assert "javascript" in res.headers["content-type"]


def test_package_data_ships_every_static_and_template_file():
    """A regular (non-editable) install only contains files matched by the package-data globs."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    package_dir = root / "src" / "penguwatch"
    patterns = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"]["setuptools"]["package-data"]["penguwatch"]
    shipped = {p for pattern in patterns for p in package_dir.glob(pattern) if p.is_file()}
    assets = {
        p for sub in ("web/static", "web/templates")
        for p in (package_dir / sub).rglob("*") if p.is_file() and "__pycache__" not in p.parts
    }
    missing = sorted(str(p.relative_to(package_dir)) for p in assets - shipped)
    assert missing == [], f"not covered by [tool.setuptools.package-data]: {missing}"


def test_no_inline_event_handlers_in_any_static_js():
    """The CSP forbids inline handlers; scan every shipped script (app.js is being split into modules)."""
    import re
    from pathlib import Path

    pattern = re.compile(r'\son(click|change|error|input|submit|load)="')
    files = sorted(Path(STATIC_DIR).rglob("*.js"))
    assert files, "no static scripts found"
    offenders = [str(p) for p in files if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
