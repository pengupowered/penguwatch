"""Installable app: manifest, service worker, CSP, token access, and the worker's caching rules."""

import json
import re

from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.main import ASSET_VERSION, STATIC_DIR, app, security_config
from penguwatch.security import CSP


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_manifest_content_type_and_shape():
    async with client() as ac:
        res = await ac.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(res.text)
    assert data["display"] == "standalone" and data["start_url"] == "/"
    sizes = {i["sizes"] for i in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in data["icons"]:  # every icon exists and is public
        assert (STATIC_DIR / icon["src"].removeprefix("/static/")).is_file()


@pytest.mark.asyncio
async def test_service_worker_headers_and_hash():
    async with client() as ac:
        res = await ac.get("/sw.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/javascript")
    assert res.headers["cache-control"] == "no-cache"
    assert ASSET_VERSION in res.text
    assert "__ASSET_VERSION__" not in res.text and "__SHELL_URLS__" not in res.text
    assert f"/static/v/{ASSET_VERSION}/js/main.js" in res.text
    assert f"/static/v/{ASSET_VERSION}/offline.html" in res.text


def test_csp_allows_worker_and_manifest_only():
    assert "worker-src 'self'" in CSP and "manifest-src 'self'" in CSP
    assert "script-src 'self';" in CSP and "unsafe-eval" not in CSP


@pytest.mark.asyncio
async def test_pwa_files_reachable_without_token_but_api_is_not(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    async with client() as ac:
        for path in ("/manifest.webmanifest", "/sw.js", f"/static/v/{ASSET_VERSION}/offline.html",
                     "/static/icons/app-192.png", "/static/icons/favicon.svg"):
            assert (await ac.get(path)).status_code == 200, path
        assert (await ac.get("/api/shows")).status_code == 401


@pytest.mark.asyncio
async def test_index_links_manifest():
    async with client() as ac:
        html = (await ac.get("/")).text
    assert 'rel="manifest"' in html and 'name="theme-color"' in html


def test_worker_never_caches_api_login_or_writes():
    source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert "/api/" not in source
    assert '"/login"' in source  # explicitly left alone
    assert 'req.method !== "GET"' in source
    assert "cache.put" not in source  # only the install-time shell list is ever stored
    assert re.search(r"startsWith\(\"/static/\"\)", source)


def test_offline_page_is_static_and_csp_safe():
    page = (STATIC_DIR / "offline.html").read_text(encoding="utf-8")
    assert "onclick" not in page and "<script" not in page


_PNG_CHUNKS_ALLOWED = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}


def _png_chunks(data: bytes) -> set:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, i = set(), 8
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        chunks.add(data[i + 4:i + 8])
        i += 12 + n
    return chunks


def _published_images():
    root = Path(__file__).resolve().parent.parent
    return sorted([*(root / "src/penguwatch/web/static").rglob("*.png"), *(root / "docs").rglob("*.png"),
                   *(root / "src/penguwatch/web/static").rglob("*.ico")])


@pytest.mark.parametrize("path", _published_images(), ids=lambda p: p.name)
def test_published_images_carry_no_metadata(path):
    """No EXIF, text, time or provenance chunks (camera, software, C2PA) in icons and screenshots."""
    data = path.read_bytes()
    if path.suffix == ".ico":
        count = int.from_bytes(data[4:6], "little")
        for k in range(count):
            entry = data[6 + 16 * k:22 + 16 * k]
            size, offset = int.from_bytes(entry[8:12], "little"), int.from_bytes(entry[12:16], "little")
            frame = data[offset:offset + size]
            if frame[:4] == b"\x89PNG":
                assert _png_chunks(frame) <= _PNG_CHUNKS_ALLOWED, path
        return
    assert _png_chunks(data) <= _PNG_CHUNKS_ALLOWED, path
