"""Themes (docs/themes.md): every theme folder passes the rules every theme must hold, the server default
(config.yaml display.theme) reaches the page, and the boot script links only our own theme files.

A new theme folder is tested here without a new test: `test_every_theme_folder` runs each one through the checks."""

import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from penguwatch.config import AppConfig, DisplayConfig, load_config
from penguwatch.main import ASSET_VERSION, STATIC_DIR, app
from penguwatch.pwa import shell_urls
from penguwatch.web import theme_checks, themes

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = sorted(p for p in themes.THEMES_DIR.iterdir() if p.is_dir())
IDS = ["default", "nord", "dracula", "solarized", "e-ink", "high-contrast", "blueprint"]


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _nord() -> themes.Theme:
    return next(t for t in themes.builtin_themes() if t.id == "nord")


def _theme_json(**tokens_dark) -> dict:
    """Nord's theme.json with changes to its dark tokens."""
    raw = json.loads((themes.THEMES_DIR / "nord" / "theme.json").read_text(encoding="utf-8"))
    raw["tokens"]["dark"] = {**raw["tokens"]["dark"], **tokens_dark}
    return raw


# ---------- every theme folder ----------

def test_the_seven_themes_are_there():
    assert [p.name for p in FOLDERS] == sorted(IDS)
    assert themes.builtin_ids()[0] == "default" and sorted(themes.builtin_ids()) == sorted(IDS)


@pytest.mark.parametrize("folder", FOLDERS, ids=[f.name for f in FOLDERS])
def test_every_theme_folder(folder):
    """The folder's theme.json follows the contract, and every mode passes the checks: text contrast, status words and
    chips, the poster pairs, the card edge, status colours apart, series colours apart (also under deuteranopia and
    protanopia)."""
    theme, problems = themes.check_theme(json.loads((folder / "theme.json").read_text(encoding="utf-8")), folder=folder)
    assert problems == []
    assert theme.id in themes.builtin_ids()  # discovered (a folder that fails discovery is left out of the page)
    assert sorted(p.name for p in folder.iterdir()) == ["theme.json"]  # palettes only: no skin, effect or views
    assert theme_checks.problems(theme) == []


def test_the_default_theme_is_tokens_css():
    default = themes.builtin_themes()[0]
    assert default.id == "default" and default.name == "PenguWatch" and default.modes == ("dark", "light")
    assert default.resolved("dark")["--bg"] == "#0a0e13" and default.resolved("light")["--bg"] == "#f1f4f7"
    assert themes.theme_css(default) == ""  # nothing over tokens.css
    assert "default" not in themes.look_stylesheets()


def test_the_checks_catch_each_rule():
    """Each kind of check fails on a palette made to break it (so a theme that passes passed something)."""
    tokens = _nord().resolved("dark")
    assert theme_checks.mode_problems(tokens) == []
    broken = {
        "text": {"--ink-3": tokens["--surface-2"]},
        "on accent": {"--on-accent": tokens["--accent"]},
        "status": {"--warn": tokens["--good"]},
        "status word": {"--serious": tokens["--serious-bg"]},
        "series": {"--s2": tokens["--s1"]},
        # red and green that only colour-blind eyes confuse: apart for normal vision, together under deuteranopia
        "colour blind": {"--s1": "#c8553d", "--s2": "#6f9a3b"},
        "poster": {"--poster-bg": tokens["--ink-4"]},
        "over image": {"--on-image": "#808080"},
        "card edge": {"--card-line": tokens["--bg"]},
    }
    found = {k: theme_checks.mode_problems({**tokens, **v}) for k, v in broken.items()}
    assert any("--ink-3 on" in p for p in found["text"])
    assert any("--on-accent on --accent" in p for p in found["on accent"])
    assert any("--good and --warn" in p for p in found["status"])
    assert any("--serious on" in p for p in found["status word"])
    assert any("--s1 and --s2 are" in p and "under" not in p for p in found["series"])
    assert any("under deuteranopia" in p for p in found["colour blind"])
    assert not any("--s1 and --s2 are" in p and "under" not in p for p in found["colour blind"])
    assert any("--ink-4 on --poster-bg" in p for p in found["poster"])
    assert any("--on-image on --on-image-bg" in p for p in found["over image"])
    assert any("--card-line on --bg" in p for p in found["card edge"])


def test_the_poster_pair_is_the_same_in_every_theme():
    """The image decides the ground under --on-image, so no theme can change it (and the default passes on white and black)."""
    for theme in themes.builtin_themes():
        for mode in theme.modes:
            r = theme.resolved(mode)
            assert (r["--on-image"], r["--on-image-bg"], r["--on-image-line"]) == ("#f4f6f8", "rgba(8, 11, 16, .74)", "rgba(255, 255, 255, .16)")


# ---------- what a theme.json may hold ----------

@pytest.mark.parametrize("change, words", [
    ({"--bg": "red; } body { display: none"}, "--bg: must be a colour"),
    ({"--accent-bg": "url(https://example.com/x)"}, "--accent-bg: must be a colour"),
    ({"--font": "Georgia"}, "--font: a theme sets colours only"),
    ({"--fs-md": "90px"}, "--fs-md: a theme sets colours only"),
    ({"--made-up": "#000000"}, "--made-up: not a theme token"),
    ({"--on-image": "#000000"}, "--on-image: the same in every theme"),
    ({"--poster-ratio": "1 / 1"}, "--poster-ratio: the same in every theme"),
    ({"--card-line": "none"}, "--card-line: must be a colour"),
    ({"--shadow-card": "0 0 0 1px red; } x { y: z"}, "--shadow-card: must be none or a box-shadow"),
    ({"--shadow-card": "url(https://example.com/x)"}, "--shadow-card: must be none or a box-shadow"),
])
def test_a_theme_takes_colours_and_two_shadow_words_only(change, words):
    theme, problems = themes.check_theme(_theme_json(**change))
    assert theme is None and any(words in p for p in problems), problems


def test_a_theme_must_give_every_colour_and_nothing_unknown():
    raw = _theme_json()
    del raw["tokens"]["dark"]["--bg"]
    assert any("missing colours --bg" in p for p in themes.check_theme(raw)[1])
    assert any("unknown field 'effect'" in p for p in themes.check_theme({**_theme_json(), "effect": None})[1])
    assert any("unknown field 'chart'" in p for p in themes.check_theme({**_theme_json(), "chart": {}})[1])
    assert themes.check_theme(_theme_json(**{"--card-line": "#445566", "--shadow-card": "none", "--grid": "rgba(1, 2, 3, .1)"}))[1] == []
    assert any("id: lower-case" in p for p in themes.check_theme({**_theme_json(), "id": "nord\n"})[1])
    assert any("must be the folder's name" in p for p in themes.check_theme(_theme_json(), folder=themes.THEMES_DIR / "dracula")[1])


# ---------- PenguWatch's own tokens ----------

def test_the_penguwatch_tokens_are_made_from_the_palette_when_it_leaves_them_out():
    nord = _nord()
    for mode in nord.modes:
        r = nord.resolved(mode)
        assert r["--poster-bg"] == r["--surface-3"]
        assert r["--card-line"] == themes.mix(r["--line-strong"], r["--ink-3"], .3)
        assert r["--card-line-hover"] == themes.mix(r["--line-strong"], r["--ink-3"], .6)
        assert r["--grid"] == "rgba(0, 0, 0, 0)"
    assert nord.resolved("dark")["--shadow-card"] == "none" and "rgba(15, 23, 32" in nord.resolved("light")["--shadow-card"]
    css = themes.look_css(nord)
    for name in themes.OPTIONAL:
        assert css.count(f" {name}:") == 2 + 1  # dark, the light of the system, the light picked
    for name in themes.FIXED:
        assert name not in css  # the same in every theme: tokens.css only
    # a palette that sets one keeps it
    own = themes.Theme(id="x", name="X", description="", modes=("dark",), tokens={"dark": {**_nord().tokens["dark"], "--card-line": "#445566"}})
    assert own.resolved("dark")["--card-line"] == "#445566" and "--card-line: #445566;" in themes.theme_css(own)


def test_blueprint_draws_its_grid_through_a_token_only():
    blueprint = next(t for t in themes.builtin_themes() if t.id == "blueprint")
    assert blueprint.tokens["dark"]["--grid"] == "rgba(255, 255, 255, 0.07)"
    components = (STATIC_DIR / "components.css").read_text(encoding="utf-8")
    assert components.count("var(--grid)") == 4 and "linear-gradient" in components  # lines are drawn from the token
    assert not (themes.THEMES_DIR / "blueprint" / "skin.css").exists()


def test_the_generated_blocks_follow_the_modes():
    css = themes.theme_css(_nord())
    assert css.startswith(':root[data-look="nord"] { color-scheme: dark;')
    assert '@media (prefers-color-scheme: light) {\n  :root[data-look="nord"]:not([data-theme="dark"]) { color-scheme: light;' in css
    assert ':root[data-look="nord"][data-theme="light"] { color-scheme: light;' in css
    one = themes.theme_css(next(t for t in themes.builtin_themes() if t.id == "e-ink"))
    assert one.count("color-scheme") == 1 and "color-scheme: light" in one and "@media" not in one


# ---------- the server default (config.yaml display.theme) ----------

def test_display_theme_in_the_config():
    assert AppConfig().display.theme == "default"
    assert AppConfig(display={"theme": "nord"}).display.theme == "nord"
    # an id no theme has loads (themes are folders, found at run time); the page falls back and logs
    assert DisplayConfig(theme="no-such-theme").theme == "no-such-theme"
    for bad in ("nord\n", "Nord", "nord;", "../nord", "", "x" * 41, " nord"):
        with pytest.raises(ValidationError, match="display.theme: a theme id"):
            DisplayConfig(theme=bad)


def test_display_theme_from_a_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("display:\n  theme: dracula\n", encoding="utf-8")
    assert load_config(str(path)).display.theme == "dracula"
    path.write_text("display:\n  theme: 'a b'\n", encoding="utf-8")
    # since the settings store, config.yaml is lenient outside the bootstrap keys: the bad id is left out (logged)
    assert load_config(str(path)).display.theme == "default"


def test_the_example_config_names_a_real_theme():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["display"]["theme"] in themes.builtin_ids()
    for theme_id in re.findall(r"^#?\s*theme: (\S+)", text, re.M):  # the commented examples too
        assert theme_id.strip('"') in themes.builtin_ids()


def test_an_unknown_default_falls_back_with_one_warning(caplog):
    themes._warned.discard("gone")
    with caplog.at_level(logging.WARNING, logger="penguwatch.web.themes"):
        assert [themes.resolve_default("gone") for _ in range(3)] == ["default"] * 3
        assert themes.resolve_default("nord") == "nord"
    warnings = [r.getMessage() for r in caplog.records if "'gone'" in r.getMessage()]
    assert len(warnings) == 1 and "display.theme" in warnings[0] and "using 'default'" in warnings[0]


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.html = {}
        self.order = []  # (tag, key attribute) of the head's scripts and stylesheets, in order

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html = a
        elif tag == "script":
            self.order.append(("script", a.get("src")))
        elif tag == "link" and a.get("rel") == "stylesheet":
            self.order.append(("css", a.get("href")))


async def _page():
    async with client() as ac:
        res = await ac.get("/")
    assert res.status_code == 200
    parsed = _Tags()
    parsed.feed(res.text)
    return res.text, parsed


@pytest.mark.asyncio
async def test_the_page_carries_the_themes_and_the_server_default(container):
    container.config.display = DisplayConfig(theme="nord")
    _, page = await _page()
    looks = json.loads(page.html["data-looks"])
    assert [t["id"] for t in looks] == list(themes.builtin_ids())
    assert page.html["data-look-default"] == "nord"
    by_id = {t["id"]: t for t in looks}
    assert "css" not in by_id["default"] and by_id["nord"]["css"] == f"/static/v/{ASSET_VERSION}/themes/nord/theme.css"
    assert by_id["e-ink"]["modes"] == ["light"] and by_id["nord"]["modes"] == ["dark", "light"]


@pytest.mark.asyncio
async def test_a_default_that_is_not_a_theme_shows_the_standard_one(container):
    container.config.display = DisplayConfig(theme="not-a-theme")
    _, page = await _page()
    assert page.html["data-look-default"] == "default"


@pytest.mark.asyncio
async def test_the_boot_script_comes_after_the_shared_stylesheets_and_before_the_panels():
    """It writes the theme's link where it stands, so the theme's sheet comes after tokens.css and the shared styles."""
    _, page = await _page()
    order = [(kind, src.rsplit(f"/{ASSET_VERSION}/", 1)[-1]) for kind, src in page.order]
    boot = order.index(("script", "theme.js"))
    assert [o for o in order[:boot]] == [("css", f) for f in ("tokens.css", "components.css", "page.css", "style.css")]
    assert order[boot + 1][0] == "css" and order[boot + 1][1].startswith("panels/")
    assert [src for kind, src in order if kind == "script"] == ["theme.js", "js/main.js"]


@pytest.mark.asyncio
async def test_api_themes_lists_every_theme_and_the_default(container):
    container.config.display = DisplayConfig(theme="solarized")
    async with client() as ac:
        data = (await ac.get("/api/themes")).json()
    assert data["default"] == "solarized"
    assert [t["id"] for t in data["themes"]] == list(themes.builtin_ids()) and data["themes"][0]["id"] == "default"
    nord = next(t for t in data["themes"] if t["id"] == "nord")
    assert nord["name"] == "Nord" and nord["modes"] == ["dark", "light"] and "arctic" in nord["description"]
    assert nord["css"] == f"/static/v/{ASSET_VERSION}/themes/nord/theme.css"
    container.config.display = DisplayConfig(theme="not-a-theme")
    async with client() as ac:
        assert (await ac.get("/api/themes")).json()["default"] == "default"


@pytest.mark.asyncio
async def test_a_theme_stylesheet_is_generated_and_served_under_the_tree_hash():
    async with client() as ac:
        res = await ac.get(f"/static/v/{ASSET_VERSION}/themes/nord/theme.css")
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/css")
        assert res.text == themes.look_css(_nord()) and res.text.startswith("/* generated from static/themes/nord/theme.json")
        assert "immutable" in res.headers["cache-control"]
        assert (await ac.get(f"/static/v/{ASSET_VERSION}/themes/default/theme.css")).status_code == 404  # tokens.css
        assert (await ac.get(f"/static/v/{ASSET_VERSION}/themes/nope/theme.css")).status_code == 404
        assert (await ac.get("/static/v/0000000000/themes/nord/theme.css")).status_code == 404


def test_the_service_worker_shell_lists_every_theme_stylesheet():
    urls = shell_urls(STATIC_DIR, ASSET_VERSION)
    for theme_id in themes.look_stylesheets():
        assert f"/static/v/{ASSET_VERSION}/themes/{theme_id}/theme.css" in urls
    assert not any("/themes/default/" in u for u in urls)


# ---------- the boot script (f2da081) ----------

BOOT = STATIC_DIR / "theme.js"
OURS = re.compile(r"var ours = function \(url\) \{\s*return typeof url === 'string' && /(.+)/\.test\(url\);")


def _js_regex(source: str) -> str:
    """A JavaScript regex literal's source as a Python pattern: `$` is the end of the text only (Python's also matches
    before a final line break), and `.test(url)` is a search, so the tests below use `.search`."""
    return source.replace(r"\/", "/").replace("$", r"\Z")


def _ours(source: str) -> "re.Pattern":
    found = OURS.search(source)
    assert found, "the check named `ours` moved: update this test with it"
    return re.compile(_js_regex(found.group(1)))


def test_theme_boot_links_every_theme_file_the_list_names_and_nothing_else():
    # static/theme.js links a stylesheet only from our own theme folders: every URL the list holds must pass its check (or
    # that theme would quietly lose its colours), and a URL from anywhere else must not
    ours = _ours(BOOT.read_text(encoding="utf-8"))
    urls = [t["css"] for t in themes.page_list(ASSET_VERSION) if "css" in t]
    assert len(urls) == 6 and all(ours.search(u) for u in urls)
    for foreign in ("https://example.com/themes/x/theme.css", "//example.com/static/v/1/themes/x/theme.css",
                    "javascript:alert(1)//.css", f"/static/v/{ASSET_VERSION}/themes/x/../../js/main.js",
                    f"/static/v/{ASSET_VERSION}/js/main.js", f"/static/v/{ASSET_VERSION}/themes/x/theme.js",
                    f"/static/v/{ASSET_VERSION}/themes/x/y/theme.css", "/static/themes/nord/theme.css",
                    f'/static/v/{ASSET_VERSION}/themes/x/"><b>.css', f"/static/v/{ASSET_VERSION}/themes/nord/theme.css\n"):
        assert not ours.search(foreign), foreign


def test_the_menu_applies_the_same_rule_as_the_boot_script():
    boot = _ours(BOOT.read_text(encoding="utf-8"))
    menu = re.search(r'const ours = \(url\) => typeof url === "string" && /(.+)/\.test\(url\);',
                     (STATIC_DIR / "js" / "themes.js").read_text(encoding="utf-8"))
    assert menu and re.compile(_js_regex(menu.group(1))).pattern == boot.pattern


def test_the_boot_script_is_a_classic_script_that_writes_one_link():
    source = BOOT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\b", source, re.M)  # a module would run only after parsing
    assert source.count("document.write(") == 1 and "look.css" in source
    assert "penguwatch.look" in source and "penguwatch.mode" in source and "penguwatch.theme" in source  # + the migration
