"""Themes (docs/themes.md): every theme folder passes the rules every theme must hold, its skin and effect keep to their
contracts, the server default (config.yaml display.theme) and the effects setting (display.effects) reach the page, the
boot script and the menu load only our own theme files, and the page keeps the hooks a skin relies on.

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
THEMES_DIR = themes.THEMES_DIR  # the shipped folders (a test below points themes.THEMES_DIR elsewhere for a while)
FOLDERS = sorted(p for p in THEMES_DIR.iterdir() if p.is_dir())
IDS = ["default", "amber-crt", "blueprint", "catppuccin", "cinema", "dracula", "e-ink", "gruvbox", "high-contrast",
       "nord", "one-dark", "rose-pine", "solarized", "starship-console", "synthwave", "tokyo-night", "video-store"]
SKINNED = ["amber-crt", "cinema", "starship-console", "synthwave", "video-store"]  # the themes with a skin.css and an effect.js


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _nord() -> themes.Theme:
    return next(t for t in themes.builtin_themes() if t.id == "nord")


def _theme_json(**tokens_dark) -> dict:
    """Nord's theme.json with changes to its dark tokens."""
    raw = json.loads((THEMES_DIR / "nord" / "theme.json").read_text(encoding="utf-8"))
    raw["tokens"]["dark"] = {**raw["tokens"]["dark"], **tokens_dark}
    return raw


# ---------- every theme folder ----------

def test_the_themes_are_there():
    assert [p.name for p in FOLDERS] == sorted(IDS)
    assert themes.builtin_ids()[0] == "default" and sorted(themes.builtin_ids()) == sorted(IDS)


def test_the_settings_choices_and_the_api_list_are_the_theme_folders():
    """Neither the Settings tab's "Theme for new browsers" nor GET /api/themes names a theme itself."""
    from penguwatch.settings import registry, schema

    spec = schema.setting_spec(next(s for s in registry.SETTINGS if s.key == "display.theme"))
    assert spec["choices"] == list(themes.builtin_ids()) and sorted(spec["choices"]) == sorted(IDS)
    assert [t["id"] for t in themes.api_list("v")] == list(themes.builtin_ids())


@pytest.mark.parametrize("folder", FOLDERS, ids=[f.name for f in FOLDERS])
def test_every_theme_folder(folder):
    """The folder's theme.json follows the contract, and every mode passes the checks: text contrast, status words and
    chips, the poster pairs, the card edge, status colours apart, series colours apart (also under deuteranopia and
    protanopia)."""
    theme, problems = themes.check_theme(json.loads((folder / "theme.json").read_text(encoding="utf-8")), folder=folder)
    assert problems == []
    assert theme.id in themes.builtin_ids()  # discovered (a folder that fails discovery is left out of the page)
    assert themes.folder_problems(folder) == []  # theme.json, and skin.css and effect.js when it has them: nothing else
    assert (theme.skin, theme.effect) == ((folder.name in SKINNED,) * 2)
    if theme.skin:
        assert themes.skin_problems(theme.id, (folder / "skin.css").read_text(encoding="utf-8")) == []
    if theme.effect:
        assert themes.effect_problems((folder / "effect.js").read_text(encoding="utf-8")) == []
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
    assert not (themes.THEMES_DIR / "blueprint" / "skin.css").exists()  # a palette: the grid needs no skin


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
    assert "skin" not in by_id["nord"] and "effect" not in by_id["nord"]  # a palette
    assert by_id["synthwave"]["skin"] == f"/static/v/{ASSET_VERSION}/themes/synthwave/skin.css"
    assert by_id["synthwave"]["effect"] == f"/static/v/{ASSET_VERSION}/themes/synthwave/effect.js"
    assert page.html["data-effects-default"] == "on"  # display.effects
    container.config.display = DisplayConfig(theme="nord", effects=False)
    _, page = await _page()
    assert page.html["data-effects-default"] == "off"


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
    assert data["default"] == "solarized" and data["effects"] is True
    assert [t["id"] for t in data["themes"]] == list(themes.builtin_ids()) and data["themes"][0]["id"] == "default"
    nord = next(t for t in data["themes"] if t["id"] == "nord")
    assert nord["name"] == "Nord" and nord["modes"] == ["dark", "light"] and "arctic" in nord["description"]
    assert nord["css"] == f"/static/v/{ASSET_VERSION}/themes/nord/theme.css" and nord["skin"] == nord["effect"] == ""
    synthwave = next(t for t in data["themes"] if t["id"] == "synthwave")
    assert synthwave["skin"].endswith("/themes/synthwave/skin.css") and synthwave["effect"].endswith("/themes/synthwave/effect.js")
    container.config.display = DisplayConfig(theme="not-a-theme", effects=False)
    async with client() as ac:
        data = (await ac.get("/api/themes")).json()
    assert data["default"] == "default" and data["effects"] is False


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


def test_the_service_worker_shell_lists_every_theme_file():
    urls = shell_urls(STATIC_DIR, ASSET_VERSION)
    for theme_id in themes.look_stylesheets():
        assert f"/static/v/{ASSET_VERSION}/themes/{theme_id}/theme.css" in urls
    for theme_id in SKINNED:
        for name in ("skin.css", "effect.js"):
            assert f"/static/v/{ASSET_VERSION}/themes/{theme_id}/{name}" in urls
    assert not any("/themes/default/" in u for u in urls)


@pytest.mark.asyncio
async def test_a_skin_and_an_effect_are_served_as_they_are_under_the_tree_hash():
    async with client() as ac:
        for name, kind in (("skin.css", "text/css"), ("effect.js", "javascript")):
            res = await ac.get(f"/static/v/{ASSET_VERSION}/themes/synthwave/{name}")
            assert res.status_code == 200 and kind in res.headers["content-type"], name
            assert res.text == (themes.THEMES_DIR / "synthwave" / name).read_text(encoding="utf-8")
        assert (await ac.get(f"/static/v/{ASSET_VERSION}/themes/nord/skin.css")).status_code == 404


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


FOREIGN = ("https://example.com/themes/x/theme.css", "//example.com/static/v/1/themes/x/theme.css",
           "javascript:alert(1)//.css", f"/static/v/{ASSET_VERSION}/themes/x/../../js/main.js",
           f"/static/v/{ASSET_VERSION}/js/main.js", f"/static/v/{ASSET_VERSION}/themes/x/y/theme.css",
           "/static/themes/nord/theme.css", f'/static/v/{ASSET_VERSION}/themes/x/"><b>.css',
           f"/static/v/{ASSET_VERSION}/themes/nord/theme.css\n", f"/static/v/{ASSET_VERSION}/themes/nord/skin.css?x=1",
           f"/static/v/{ASSET_VERSION}/themes/x/../../../static/v/1/themes/x/skin.css", f"data:text/css,body{{}}")


def test_theme_boot_links_every_theme_file_the_list_names_and_nothing_else():
    # static/theme.js links a stylesheet (the token blocks and the skin) only from our own theme folders: every URL the list
    # holds must pass its check (or that theme would quietly lose its colours or its skin), and a URL from anywhere else
    # must not
    ours = _ours(BOOT.read_text(encoding="utf-8"))
    looks = themes.page_list(ASSET_VERSION)
    urls = [t["css"] for t in looks if "css" in t]
    skins = [t["skin"] for t in looks if "skin" in t]
    assert len(urls) == len(IDS) - 1 and len(skins) == len(SKINNED) and all(ours.search(u) for u in urls + skins)
    for foreign in (*FOREIGN, f"/static/v/{ASSET_VERSION}/themes/x/theme.js", f"/static/v/{ASSET_VERSION}/themes/x/effect.js"):
        assert not ours.search(foreign), foreign


def _menu_rule(name: str) -> "re.Pattern":
    found = re.search(rf'const {name} = \(url\) => typeof url === "string" && /(.+)/\.test\(url\);',
                      (STATIC_DIR / "js" / "themes.js").read_text(encoding="utf-8"))
    assert found, f"the check named `{name}` in js/themes.js moved: update this test with it"
    return re.compile(_js_regex(found.group(1)))


def test_the_menu_runs_only_our_own_effect_files():
    """js/themes.js imports a theme's effect only from its own theme folder, under the tree hash, named effect.js: every
    effect the list names passes, and no other script, stylesheet or site does."""
    ours = _menu_rule("ourEffect")
    effects = [t["effect"] for t in themes.page_list(ASSET_VERSION) if "effect" in t]
    assert len(effects) == len(SKINNED) and all(ours.search(u) for u in effects)
    for foreign in (*FOREIGN, *(u.replace(".css", ".js") for u in FOREIGN), f"/static/v/{ASSET_VERSION}/themes/x/skin.css",
                    f"/static/v/{ASSET_VERSION}/themes/x/other.js", f"/static/v/{ASSET_VERSION}/js/themes.js",
                    "https://example.com/static/v/1/themes/x/effect.js", f"/static/v/{ASSET_VERSION}/themes/x/effect.js\n",
                    f"/static/v/{ASSET_VERSION}/themes/x/effect.mjs", "blob:https://example.com/effect.js"):
        assert not ours.search(foreign), foreign


def test_the_menu_applies_the_same_rule_as_the_boot_script():
    assert _menu_rule("ours").pattern == _ours(BOOT.read_text(encoding="utf-8")).pattern


def test_the_boot_script_is_a_classic_script_that_writes_one_link():
    source = BOOT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\b", source, re.M)  # a module would run only after parsing
    assert source.count("document.write(") == 1 and "sheet(look.css," in source and "sheet(look.skin," in source
    assert "effect" not in re.sub(r"//[^\n]*", "", source)  # the effect is the module's (js/themes.js), never run here
    assert "penguwatch.look" in source and "penguwatch.mode" in source and "penguwatch.theme" in source  # + the migration


# ---------- skins and effects (docs/themes.md, "Skins" and "Effects") ----------

SCOPE = ':root[data-look="testskin"]'
GOOD_SKIN = f"""/* a skin */
{SCOPE} {{ --ts-glow: color-mix(in srgb, var(--accent) 40%, transparent); }}
{SCOPE} .panel, {SCOPE} :is(.upnext-card, .show-card) {{ border-color: var(--accent); box-shadow: 0 0 8px var(--ts-glow); }}
@media (max-width: 720px) {{ {SCOPE} .nav-btn.active::before {{ background: var(--accent); }} }}
@supports (backdrop-filter: blur(1px)) {{ {SCOPE} .topbar::before {{ backdrop-filter: blur(3px); }} }}
@keyframes testskin-pulse {{ from {{ opacity: 1; }} to {{ opacity: .8; }} }}
{SCOPE} .fx-layer canvas {{ position: absolute; will-change: transform; }}
"""
GOOD_EFFECT = """// an effect
export default {
  start(layer, ctx) {
    const canvas = document.createElement("canvas");
    layer.append(canvas);
    const g = canvas.getContext("2d");
    const off = ctx.onFrame(() => { g.fillStyle = ctx.tokens()["--accent"]; g.fillRect(0, 0, 1, 1); }, 4);
    return () => { off(); canvas.remove(); };
  },
};
"""


def _theme_folder(root: Path, theme_id: str = "testskin", skin: str = GOOD_SKIN, effect: str = GOOD_EFFECT) -> Path:
    folder = root / theme_id
    folder.mkdir(parents=True)
    (folder / "theme.json").write_text(json.dumps({**_theme_json(), "id": theme_id, "name": "Test skin"}), encoding="utf-8")
    if skin is not None:
        (folder / "skin.css").write_text(skin, encoding="utf-8")
    if effect is not None:
        (folder / "effect.js").write_text(effect, encoding="utf-8")
    return folder


@pytest.fixture
def theme_root(tmp_path, monkeypatch):
    """themes.THEMES_DIR pointed at an empty folder with only `default`, the caches cleared before and after."""
    root = tmp_path / "themes"
    (root / "default").mkdir(parents=True)
    (root / "default" / "theme.json").write_text((THEMES_DIR / "default" / "theme.json").read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    monkeypatch.setattr(themes, "THEMES_DIR", root)
    themes.builtin_themes.cache_clear()
    themes.look_stylesheets.cache_clear()
    yield root
    themes.builtin_themes.cache_clear()
    themes.look_stylesheets.cache_clear()


def test_the_loader_accepts_a_skin_and_an_effect(theme_root):
    folder = _theme_folder(theme_root)
    theme, problems = themes.check_theme(json.loads((folder / "theme.json").read_text(encoding="utf-8")), folder=folder)
    assert problems == [] and theme.skin and theme.effect
    assert themes.skin_problems("testskin", GOOD_SKIN) == [] and themes.effect_problems(GOOD_EFFECT) == []
    assert themes.builtin_ids() == ("default", "testskin")
    entry = next(e for e in themes.page_list("v1") if e["id"] == "testskin")
    assert entry == {"id": "testskin", "name": "Test skin", "modes": ["dark", "light"], "css": "/static/v/v1/themes/testskin/theme.css",
                     "skin": "/static/v/v1/themes/testskin/skin.css", "effect": "/static/v/v1/themes/testskin/effect.js"}
    assert themes.theme_files("v1") == [entry["css"], entry["skin"], entry["effect"]]
    # a palette folder has neither, and the list says so by leaving the keys out
    _theme_folder(theme_root, "plain", skin=None, effect=None)
    themes.builtin_themes.cache_clear()
    plain = next(e for e in themes.page_list("v1") if e["id"] == "plain")
    assert "skin" not in plain and "effect" not in plain


@pytest.mark.parametrize("skin, words", [
    (".panel { border-color: var(--accent); }", "'.panel' is not under"),  # unscoped
    (f"{SCOPE} .panel, .show-card {{ border-color: var(--accent); }}", "'.show-card' is not under"),  # one of a list
    (f"{SCOPE}:is(.a, .b), body {{ color: var(--ink-1); }}", "'body' is not under"),  # a comma inside :is() is not a split
    (f"@media (min-width: 1px) {{ .panel {{ color: var(--ink-1); }} }}", "'.panel' is not under"),  # inside @media
    (f"@supports (x: y) {{ @media print {{ body {{ color: var(--ink-1); }} }} }}", "'body' is not under"),
    (':root[data-look="nord"] .panel { color: var(--ink-1); }', "is not under"),  # another theme's scope
    ('[data-look="testskin"] .panel { color: var(--ink-1); }', "is not under"),  # not rooted at :root
    (':root[data-look="testskin-x"] .panel { color: var(--ink-1); }', "is not under"),  # a longer id
    (':root[data-look="testskin" i] .panel { color: var(--ink-1); }', "is not under"),
    ('html .panel { color: var(--ink-1); }', "is not under"),
    (f"{SCOPE} .panel {{ & + body {{ color: var(--ink-1); }} }}", "no nested rules"),
    (f"@layer x {{ {SCOPE} .panel {{ color: var(--ink-1); }} }}", "@layer is not allowed"),
    ("@import url(other.css);", "no url(), @import"),
    (f"{SCOPE} body {{ background: url(https://example.com/x.png); }}", "no url()"),
    ("@font-face { font-family: x; }", "@font-face is not allowed"),
    ("@keyframes spin { from { opacity: 1; } }", "the name starts with 'testskin-'"),
    (f"{SCOPE} .panel {{ border-color: #ff00ff; }}", "colour '#ff00ff' written out"),
    (f"{SCOPE} .panel {{ border-color: rgba(255, 0, 255, .5); }}", "colour 'rgba(' written out"),
    (f"{SCOPE} .panel {{ border-color: hotpink; }}", "colour 'hotpink' written out"),
    (f"{SCOPE} {{ --accent: var(--s1); }}", "--accent is the palette's"),
    (f"{SCOPE} {{ --on-image-bg: transparent; }}", "--on-image-bg is the palette's (theme.json) or fixed"),
    (f"{SCOPE} {{ --card-line: var(--s1); }}", "--card-line is the palette's"),
    (f"{SCOPE} .fx-layer {{ z-index: 5; }}", "z-index on the effect layer"),
    (f"{SCOPE} #fx-layer[data-fx] {{ position: static; }}", "position on the effect layer"),
    (f"{SCOPE} .fx-layer canvas {{ pointer-events: auto; }}", "pointer-events on the effect layer"),
    (f"{SCOPE} .panel {{ color: var(--ink-1); }} }}", "an unmatched }"),
    (f"{SCOPE} .panel {{ color: var(--ink-1);", "is not closed"),
])
def test_a_skin_that_breaks_the_contract_is_refused(skin, words):
    problems = themes.skin_problems("testskin", skin)
    assert any(words in p for p in problems), problems


def test_a_theme_with_an_unscoped_skin_is_left_out_at_load(theme_root, caplog):
    _theme_folder(theme_root, "leaky", skin=GOOD_SKIN.replace(SCOPE, ':root[data-look="leaky"]') + "\n.panel { opacity: 0; }\n")
    _theme_folder(theme_root, "tidy", skin=GOOD_SKIN.replace(SCOPE, ':root[data-look="tidy"]').replace("testskin-", "tidy-"))
    with caplog.at_level(logging.ERROR, logger="penguwatch.web.themes"):
        assert themes.builtin_ids() == ("default", "tidy")
    assert any("Theme leaky left out" in r.getMessage() and "'.panel' is not under" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("change, words", [
    (("start(layer, ctx)", "begin(layer, ctx)"), "the default export is {start(layer, ctx)}"),
    (("// an effect", 'import { x } from "./other.js";'), "no import"),
    (("g.fillRect(0, 0, 1, 1);", "g.fillRect(0, 0, 1, 1); requestAnimationFrame(() => {});"), "requestAnimationFrame"),
    (("g.fillRect(0, 0, 1, 1);", "setInterval(() => {}, 10);"), "setInterval"),
    (("g.fillRect(0, 0, 1, 1);", "setTimeout(() => {}, 10);"), "setTimeout"),
    (("g.fillRect(0, 0, 1, 1);", 'eval("1");'), "eval"),
    (("g.fillRect(0, 0, 1, 1);", 'new Function("return 1")();'), "Function"),
    (("g.fillRect(0, 0, 1, 1);", 'fetch("/api/shows");'), "fetch"),
    (("g.fillRect(0, 0, 1, 1);", 'import("/x.js");'), "import"),
    (("g.fillRect(0, 0, 1, 1);", 'localStorage.setItem("a", "b");'), "localStorage"),
    (("layer.append(canvas);", "document.body.append(canvas);"), "document.body"),
    (("layer.append(canvas);", 'document.querySelector("main").append(canvas);'), "querySelector"),
    (("layer.append(canvas);", 'layer.append(canvas); canvas.style.zIndex = "9";'), "zIndex"),
    (("layer.append(canvas);", 'layer.append(canvas); canvas.style.pointerEvents = "auto";'), "pointerEvents"),
    (("layer.append(canvas);", 'layer.append(canvas); canvas.innerHTML = "<b>x</b>";'), "innerHTML"),
    (('ctx.tokens()["--accent"]', '"#ff00ff"'), "colour '#ff00ff' written out"),
    (('ctx.tokens()["--accent"]', '"rgb(255, 0, 255)"'), "colour 'rgb(' written out"),
    (('ctx.tokens()["--accent"]', 'matchMedia("(x)").matches ? "" : ""'), "matchMedia"),
])
def test_an_effect_that_breaks_the_contract_is_refused(change, words):
    old, new = change
    assert old in GOOD_EFFECT
    problems = themes.effect_problems(GOOD_EFFECT.replace(old, new))
    assert any(words in p for p in problems), problems


def test_a_theme_folder_holds_its_three_files_only(theme_root):
    folder = _theme_folder(theme_root)
    (folder / "theme.css").write_text("x", encoding="utf-8")  # would be hidden by the generated stylesheet
    (folder / "extra").mkdir()
    assert themes.folder_problems(folder) == ["extra: a theme folder holds only theme.json, skin.css, effect.js",
                                              "theme.css: a theme folder holds only theme.json, skin.css, effect.js"]
    assert "testskin" not in themes.builtin_ids()


# ---------- the effect layer ----------

class _Body(HTMLParser):
    """The rendered page as (tag, id, classes, depth) for every element of <body>, in order."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr", "use"}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.inside = False
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "body":
            self.inside = True
            return
        if not self.inside:
            return
        self.nodes.append((tag, a.get("id") or "", set((a.get("class") or "").split()), self.depth, a))
        if tag not in self.VOID:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.inside:
            a = dict(attrs)
            self.nodes.append((tag, a.get("id") or "", set((a.get("class") or "").split()), self.depth, a))

    def handle_endtag(self, tag):
        if tag == "body":
            self.inside = False
        elif self.inside and tag not in self.VOID:
            self.depth -= 1


async def _body():
    text, _ = await _page()
    parsed = _Body()
    parsed.feed(text)
    return parsed.nodes


def _children(nodes, index):
    """The direct children of nodes[index]."""
    depth = nodes[index][3]
    out = []
    for node in nodes[index + 1:]:
        if node[3] <= depth:
            break
        if node[3] == depth + 1:
            out.append(node)
    return out


@pytest.mark.asyncio
async def test_the_effect_layer_is_behind_the_page_and_never_hit():
    """#fx-layer is the first element of <body>, empty and hidden from assistive technology, and page.css keeps it fixed,
    behind everything (z-index -1 in the root's stacking context) and out of the pointer's way, its children too."""
    nodes = await _body()
    tag, ident, classes, depth, attrs = nodes[0]
    assert (tag, ident, classes, depth, attrs.get("aria-hidden")) == ("div", "fx-layer", {"fx-layer"}, 0, "true")
    assert nodes[1][3] == 0  # nothing inside it in the markup
    css = (STATIC_DIR / "page.css").read_text(encoding="utf-8")
    rule = re.search(r"^\.fx-layer \{([^}]*)\}", css, re.M).group(1)
    for decl in ("position: fixed", "inset: 0", "z-index: -1", "pointer-events: none"):
        assert decl in rule, decl
    assert re.search(r"^\.fx-layer > \* \{ pointer-events: none; \}", css, re.M)


# ---------- the page hooks a skin relies on (docs/themes.md, "Page hooks a skin may rely on") ----------

WEB = STATIC_DIR.parent
# part -> [(class, the files that make it: their markup or script writes the class)]. Every class is also a rule of the
# page's stylesheets, so a skin restyles something that is there.
PAGE_HOOKS = {
    "the effect layer": [("fx-layer", ["templates/index.html"])],
    "the header": [("topbar", ["templates/index.html"]), ("tb-inner", ["templates/index.html"]),
                   ("brand", ["templates/index.html"]),
                   ("brand-name", ["templates/index.html"]), ("status-pill", ["templates/index.html"]),
                   ("tb-actions", ["templates/index.html"])],
    "the tab bar": [("tabs", ["templates/index.html"]), ("tabs-inner", ["templates/index.html"]),
                    ("nav-btn", ["templates/index.html"]), ("nav-more", ["templates/index.html"])],
    "a tab's page": [("app-main", ["templates/index.html"]), ("tab-pane", ["static/panels/upnext/panel.html"]),
                     ("page-h", ["static/panels/upnext/panel.html"]), ("page-t", ["static/panels/upnext/panel.html"])],
    "cards": [("upnext-card", ["static/panels/upnext/panel.js"]), ("show-card", ["static/panels/watchlist/cards.js"]),
              ("discover-card", ["static/panels/discover/panel.js"]), ("timeline-card", ["static/panels/calendar/panel.js"])],
    "panels": [("panel", ["static/panels/services/panel.html", "static/panels/history/panel.js"]),
               ("panel-h", ["static/panels/history/panel.js"]), ("panel-t", ["static/panels/history/panel.js"]),
               ("panel-b", ["static/panels/history/panel.js"])],
    "chips": [("chip", ["templates/index.html", "static/panels/upnext/panel.js"])],
    "buttons": [("btn", ["templates/index.html"]), ("seg", ["static/panels/history/panel.html"])],
    "inputs": [("input", ["static/panels/settings/fields.js"])],
    "dialogs": [("modal", ["static/dialogs/details/dialog.html"]), ("modal-content", ["static/dialogs/details/dialog.html"]),
                ("modal-h", ["static/dialogs/details/dialog.html"]), ("modal-t", ["static/dialogs/details/dialog.html"]),
                ("modal-b", ["static/dialogs/details/dialog.html"])],
    "menus": [("menu", ["templates/index.html"])],
    "notes": [("empty", ["static/panels/upnext/panel.js"]), ("result", ["static/panels/upnext/panel.html"])],
    "the Settings rows": [("set-sec", ["static/panels/settings/panel.js"]), ("set-row", ["static/panels/settings/fields.js"]),
                          ("set-card", ["static/panels/settings/lists/webhooks.js"])],
    "the Settings bar": [("set-bar", ["static/panels/settings/panel.html"])],
    "History's charts": [("hc-svg", ["static/panels/history/charts.js"]), ("hc-grid", ["static/panels/history/charts.js"]),
                         ("hc-axis", ["static/panels/history/charts.js"]), ("hc-bar", ["static/panels/history/charts.js"])],
    "posters (left alone)": [("poster", ["static/panels/upnext/panel.js", "static/panels/watchlist/cards.js"]),
                             ("on-image", ["static/panels/watchlist/cards.js"])],
}


def _all_page_css() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in STATIC_DIR.rglob("*.css") if "themes" not in p.parts)


@pytest.mark.parametrize("part", PAGE_HOOKS)
def test_the_page_keeps_the_hooks_a_skin_relies_on(part):
    """A skin styles the page by these classes; a refactor that renames or drops one breaks every skin quietly, so it
    fails here instead (and docs/themes.md's table is updated with it)."""
    css = _all_page_css()
    for name, files in PAGE_HOOKS[part]:
        token = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
        for rel_path in files:
            source = (WEB / rel_path).read_text(encoding="utf-8")
            assert any(token.search(c) for c in re.findall(r'class(?:Name)?\s*=\s*["`]([^"`]*)', source)), f"{name} in {rel_path}"
        assert re.search(rf"(^|[\s,>}}]|:is\(|:not\()\.{re.escape(name)}(?![\w-])", css, re.M), f".{name} has no rule"


@pytest.mark.asyncio
async def test_the_page_keeps_the_structure_a_skin_relies_on():
    """header.topbar holds div.tb-inner (its ::before is the header's background) and nav.tabs (div.tabs-inner of
    button.nav-btn, then button.nav-more); main.app-main holds one section.tab-pane per tab, each starting with its .page-h."""
    nodes = await _body()
    index = {n[1]: i for i, n in enumerate(nodes) if n[1]}
    top = index["topbar"]
    assert nodes[top][0] == "header" and "topbar" in nodes[top][2]
    kids = _children(nodes, top)
    assert [(k[0], sorted(k[2])) for k in kids] == [("div", ["tb-inner"]), ("nav", ["tabs"])]
    tabs = index["tabs"]
    inner = _children(nodes, tabs)
    assert [k[0] for k in inner] == ["div", "button"] and "tabs-inner" in inner[0][2] and "nav-more" in inner[1][2]
    buttons = _children(nodes, nodes.index(inner[0]))
    assert len(buttons) >= 10 and all(b[0] == "button" and "nav-btn" in b[2] and b[4].get("role") == "tab" for b in buttons)
    main = index["main"]
    assert nodes[main][0] == "main" and "app-main" in nodes[main][2]
    panes = [k for k in _children(nodes, main) if k[0] == "section"]
    assert len(panes) == len(buttons) and all("tab-pane" in p[2] and p[4].get("data-panel") for p in panes)
    for pane in panes:
        first = _children(nodes, nodes.index(pane))[0]
        assert "page-h" in first[2], pane[1]


def test_the_docs_list_every_page_hook():
    docs = (ROOT / "docs" / "themes.md").read_text(encoding="utf-8")
    section = docs[docs.index("### Page hooks a skin may rely on"):]
    section = section[:section.index("\n## ")]
    for part, hooks in PAGE_HOOKS.items():
        for name, _ in hooks:
            assert f".{name}" in section, f"{part}: .{name}"
