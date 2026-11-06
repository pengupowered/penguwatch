"""The dashboard's rules, enforced (after Pengumon's tests/test_frontend.py): the look comes from the tokens, the CSP
needs no inline anything, every tab is a complete folder no shared file names, and every hook the markup names exists.
See docs/ui.md."""

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import ui
from penguwatch.main import ASSET_VERSION, app

WEB = Path(__file__).resolve().parents[1] / "src" / "penguwatch" / "web"
STATIC = WEB / "static"
JS = STATIC / "js"  # the shell and the shared library
SHARED_CSS = ("tokens.css", "components.css", "page.css", "style.css")
PANELS = sorted(p for p in (STATIC / "panels").iterdir() if p.is_dir())
DIALOGS = sorted(p for p in (STATIC / "dialogs").iterdir() if p.is_dir())


def rel(path: Path) -> str:
    return path.relative_to(WEB).as_posix()


def all_js() -> list[Path]:
    """Every module: the shell's, each tab's and each dialog's, plus the two classic scripts (theme, worker)."""
    return sorted(STATIC.rglob("*.js"))


def all_css() -> list[Path]:
    return sorted(STATIC.rglob("*.css"))


def all_html() -> list[Path]:
    """The page, its partials (the sprite), every tab's and dialog's markup and the offline page."""
    return sorted([*(WEB / "templates").rglob("*.html"), *(WEB / "templates").rglob("*.svg"),
                   *(STATIC / "panels").rglob("*.html"), *(STATIC / "dialogs").rglob("*.html"), STATIC / "offline.html"])


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def rendered_page() -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/")
    assert res.status_code == 200
    return res.text


# --- the look: tokens only ---------------------------------------------------------------------------------------

COLOUR = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])|\b(?:rgba?|hsla?)\(")


@pytest.mark.parametrize("path", [*all_css(), *all_js(), *all_html()], ids=rel)
def test_no_colour_literal_outside_the_tokens(path):
    """Every colour is a token (tokens.css), so both themes cover the whole page. The browser's theme-color meta is
    the one place that cannot read a token (chrome.js updates it from --bg)."""
    if path.name == "tokens.css":
        return
    lines = [line for line in text(path).splitlines() if 'name="theme-color"' not in line]
    assert [m.group(0) for line in lines for m in COLOUR.finditer(line)] == []


def test_the_tokens_have_both_themes():
    tokens = text(STATIC / "tokens.css")
    dark = set(re.findall(r"^\s*(--[\w-]+):", tokens[:tokens.index("@media (prefers-color-scheme: light)")], re.M))
    os_light = tokens[tokens.index("@media (prefers-color-scheme: light)"):tokens.index(':root[data-theme="light"]')]
    picked_light = tokens[tokens.index(':root[data-theme="light"]'):]
    assert ':root:not([data-theme="dark"])' in os_light  # the OS setting applies until the person picks
    light_os = set(re.findall(r"(--[\w-]+):", os_light))
    light_picked = set(re.findall(r"(--[\w-]+):", picked_light))
    assert light_os == light_picked  # the toggle and the OS setting give the same light theme
    colour_tokens = {name for name in dark if re.search(rf"{name}:\s*(#|rgba?\()", tokens)}
    assert {"--bg", "--surface-1", "--ink-1", "--accent", "--good", "--warn", "--crit", "--poster-bg"} <= light_os
    assert colour_tokens - light_os <= {"--on-image", "--on-image-bg", "--on-image-line"}  # over images: one look


def test_every_custom_property_used_is_declared():
    """A misspelt token (var(--ink-5)) silently falls back; every var() names a property something declares."""
    css = "\n".join(text(p) for p in all_css())
    js = "\n".join(text(p) for p in all_js())
    declared = set(re.findall(r"(--[\w-]+)\s*:", css)) | set(re.findall(r"""setProperty\(\s*["'](--[\w-]+)""", js))
    used = set(re.findall(r"var\(\s*(--[\w-]+)\s*\)", css))  # a var() with a fallback is a component's parameter
    assert len(used) > 40 and used - declared == set()


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _blend(rgba: str, under: str) -> str:
    r, g, b, a = (float(x) for x in re.match(r"rgba\(([^)]*)\)", rgba).group(1).split(","))
    base = [int(under[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c * a + u * (1 - a)):02x}" for c, u in zip((r, g, b), base))


def _themes() -> dict:
    tokens = text(STATIC / "tokens.css")
    dark = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", tokens[:tokens.index("@media (prefers-color-scheme: light)")]))
    light = {**dark, **dict(re.findall(r"(--[\w-]+):\s*([^;]+);", tokens[tokens.index(':root[data-theme="light"]'):]))}
    return {"dark": dark, "light": light}


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_colours_pass_aa_in_both_themes(theme):
    t = {k: v.strip() for k, v in _themes()[theme].items()}
    for surface in ("--bg", "--surface-1", "--surface-2"):
        for ink in ("--ink-1", "--ink-2", "--ink-3", "--accent"):
            assert _contrast(t[ink], t[surface]) >= 4.5, (theme, ink, surface)
    assert _contrast(t["--on-accent"], t["--accent"]) >= 4.5  # a primary button's label
    # a status word on its own tint (badges, chips, notes), over a panel
    for status in ("--good", "--warn", "--serious", "--crit", "--busy", "--unknown"):
        tint = _blend(t[f"{status}-bg"], t["--surface-1"])
        assert _contrast(t[status], tint) >= 4.5, (theme, status)
        assert _contrast(t[status], t["--surface-1"]) >= 4.5, (theme, status)
    # chips and ratings over a poster: the same in both themes, readable over a white image
    assert _contrast(t["--on-image"], _blend(t["--on-image-bg"], "#ffffff")) >= 4.5



CARD_PANELS = ("upnext", "watchlist", "discover", "calendar")  # the tabs whose cards repeat in a grid or list


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_card_edges_stand_out_from_the_page(theme):
    """The repeated cards sit in dense grids: their edge has to be visible on its own (the owner could not tell one
    Up Next card from the next with the 1.4:1 hairline)."""
    t = {k: v.strip() for k, v in _themes()[theme].items()}
    floor = 2.0 if theme == "dark" else 1.8  # light also has --shadow-card
    assert _contrast(t["--card-line"], t["--bg"]) >= floor, theme
    assert _contrast(t["--card-line"], t["--surface-1"]) >= floor, theme


@pytest.mark.parametrize("panel", CARD_PANELS)
def test_the_repeated_cards_use_the_card_edge(panel):
    css = text(STATIC / "panels" / panel / "panel.css")
    assert "border: 1px solid var(--card-line)" in css and "box-shadow: var(--shadow-card)" in css, panel


# --- the CSP: no inline style, script or handler --------------------------------------------------------------------

INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)  # in markup
INLINE_HANDLER_IN_JS = re.compile(r"""\son[a-z]+\s*=\s*\\?["'`]""", re.IGNORECASE)  # in markup a module builds
STYLE_ATTRIBUTE = re.compile(r"""\bstyle\s*=\s*\\?["'`]|setAttribute\(\s*["']style["']|<style\b""")


@pytest.mark.parametrize("path", [*all_html(), *all_js()], ids=rel)
def test_no_inline_style_attribute_or_block(path):
    """No style="" in templates or in markup the modules build (setting el.style.x from a script is fine: the CSP
    does not apply to the CSSOM); no <style> blocks."""
    assert STYLE_ATTRIBUTE.findall(text(path)) == []


@pytest.mark.parametrize("path", [*all_html(), *all_js()], ids=rel)
def test_no_inline_event_handler_attributes(path):
    pattern = INLINE_HANDLER_IN_JS if path.suffix == ".js" else INLINE_HANDLER
    assert pattern.findall(text(path)) == []
    assert "javascript:" not in text(path)


def test_templates_have_no_inline_script():
    for path in all_html():
        for tag in re.findall(r"<script\b[^>]*>", text(path)):
            assert "src=" in tag, f"{rel(path)}: {tag}"


@pytest.mark.asyncio
async def test_csp_forbids_inline_style_and_allows_only_the_sign_in_page_block(monkeypatch):
    from homeguard.security import LOGIN_PAGE
    from penguwatch.main import security_config
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")  # the sign-in page exists with a token
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        csp = (await ac.get("/login")).headers["content-security-policy"]
        login = (await ac.get("/login")).text
    style_src = next(p.strip() for p in csp.split(";") if p.strip().startswith("style-src"))
    assert "'unsafe-inline'" not in style_src and "'unsafe-hashes'" not in style_src
    # the one hash is the sign-in page's own <style> block, exactly as homeguard sends it
    blocks = re.findall(r"<style>(.*?)</style>", login, re.S)
    assert len(blocks) == 1 and blocks[0] in LOGIN_PAGE.replace("{{", "{").replace("}}", "}")
    digest = base64.b64encode(hashlib.sha256(blocks[0].encode()).digest()).decode()
    assert style_src == f"style-src 'self' 'sha256-{digest}'"
    assert not re.search(r"\sstyle\s*=", login)
    # and the sign-in page takes the dashboard's look from same-origin files
    for asset in ('<script src="/static/theme.js"></script>', 'href="/static/tokens.css"', 'href="/static/login.css"'):
        assert asset in login


# --- the tabs: one complete folder each, named by no shared file ---------------------------------------------------

def test_every_panel_folder_is_complete():
    """A tab is a folder: panel.json, panel.html (one section of that tab), panel.js (its module), panel.css."""
    sprite = set(re.findall(r'<symbol id="i-([\w-]+)"', text(WEB / "templates" / "icons.svg")))
    found = ui.discover()
    assert [p.id for p in found.panels] == [p.name for p in sorted(PANELS, key=lambda f: json.loads(text(f / "panel.json"))["order"])]
    assert len(found.panels) == 10
    routes, keys = [], []
    for part in found.panels:
        folder = STATIC / "panels" / part.id
        assert {"panel.json", "panel.html", "panel.js", "panel.css"} <= {p.name for p in folder.iterdir()}, part.id
        markup = text(folder / "panel.html")
        assert re.findall(r'<section\b[^>]*\bdata-panel="([\w-]+)"', markup) == [part.id]
        assert re.search(rf'<section id="{part.id}-tab" class="tab-pane" data-panel="{part.id}" role="tabpanel"', markup)
        module = text(folder / "panel.js")
        assert re.search(rf'export default \{{\s*id: "{part.id}",', module), part.id
        assert re.search(r"\bmount\b", module), part.id
        assert part.title and part.icon in sprite, part.id
        routes.append(part.route)
        keys.append(part.key)
    assert len(set(routes)) == len(routes) and len(set(keys)) == len(keys)
    for folder in DIALOGS:
        assert {"dialog.html", "dialog.js"} <= {p.name for p in folder.iterdir()}, folder.name
        assert re.search(rf'export default \{{ id: "{folder.name}",', text(folder / "dialog.js")), folder.name


def test_no_shared_file_names_a_tab():
    """Adding, removing or renaming a tab touches its own folder only: the page, the shell's modules and the shared
    styles never name one (its id, its section, its folder or its route)."""
    shared = {rel(WEB / "templates" / "index.html"): text(WEB / "templates" / "index.html"),
              **{rel(p): text(p) for p in JS.glob("*.js")},
              **{c: text(STATIC / c) for c in SHARED_CSS}}
    for part in ui.discover().panels:
        patterns = (rf"""["'#]{part.id}-tab\b""", rf"panels/{part.id}\b", rf'data-panel="{part.id}"', rf"#/{part.route}\b",
                    rf"""["']{part.route}["']""")
        for name, body in shared.items():
            for pattern in patterns:
                assert not re.search(pattern, body), f"{name} names the tab {part.id} ({pattern})"


def test_folders_import_only_the_shell_and_their_own_files():
    """A tab's or dialog's folder imports static/js and files of its own folder, never another folder's: removing a
    tab cannot break another one (they talk through the bus, js/bus.js, and the delegated actions)."""
    for root in (STATIC / "panels", STATIC / "dialogs"):
        for path in root.rglob("*.js"):
            folder = (root / path.relative_to(root).parts[0]).resolve()
            for imported in re.findall(r"""(?:from|import) ["'](\.{1,2}/[\w./-]+\.js)["']""", text(path)):
                target = (path.parent / imported).resolve()
                assert folder in target.parents or JS.resolve() in target.parents, f"{rel(path)} imports {imported}"
    for path in JS.glob("*.js"):
        for imported in re.findall(r"""(?:from|import) ["'](\.{1,2}/[\w./-]+\.js)["']""", text(path)):
            assert (path.parent / imported).resolve().parent == JS.resolve(), f"{rel(path)} imports {imported}"


def _exports(path: Path) -> set:
    body = text(path)
    names = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)", body))
    return names | ({"default"} if "export default" in body else set())


def test_every_named_import_exists():
    for path in all_js():
        for names, imported in re.findall(r"""import\s*\{([^}]*)\}\s*from\s*["']([^"']+)["']""", text(path)):
            target = (path.parent / imported).resolve()
            assert target.is_file(), f"{rel(path)} imports a missing {imported}"
            missing = {n.strip().split(" as ")[0] for n in names.split(",") if n.strip()} - _exports(target)
            assert not missing, f"{rel(path)} imports {sorted(missing)} that {imported} does not export"


def test_every_module_is_loaded():
    """A module nothing loads never runs: every shell module is reached from main.js, every folder module from its
    folder's entry (panel.js / dialog.js), which main.js loads from the page's data-modules."""
    entries = [JS / "main.js", *[STATIC / m for m in ui.modules() if m.endswith(("/panel.js", "/dialog.js"))]]
    seen: set[Path] = set()
    todo = list(entries)
    while todo:
        path = todo.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        for imported in re.findall(r"""(?:from|import) ["'](\.{1,2}/[\w./-]+\.js)["']""", text(path)):
            todo.append(path.parent / imported)
    modules = {p.resolve() for p in all_js() if p.name not in ("sw.js", "theme.js")}
    assert modules - seen == set()


@pytest.mark.asyncio
async def test_the_page_includes_links_and_loads_every_folder():
    html = await rendered_page()
    found = ui.discover()
    listed = json.loads(re.search(r"<body data-modules='([^']*)'", html).group(1))
    assert listed == {"dialogs": [d.module for d in found.dialogs], "panels": [p.module for p in found.panels]}
    for part in (*found.panels, *found.dialogs):
        for css in part.css:
            assert f'href="/static/v/{ASSET_VERSION}/{css}"' in html
    for part in found.panels:
        assert f'data-panel="{part.id}"' in html
        assert re.search(rf'<button type="button" class="nav-btn" role="tab" id="tab-{part.id}" data-tab="{part.id}-tab" data-route="{part.route}"', html)
    for template in found.dialog_templates:
        dialog_id = re.search(r'<div class="modal" id="([\w-]+)"', text(STATIC / template)).group(1)
        assert f'id="{dialog_id}"' in html


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["panels/watchlist/panel.html", "panels/watchlist/panel.json", "panels/watchlist/tidy.html",
                                  "dialogs/details/dialog.html", "panels/./watchlist/panel.html", "panels\\watchlist\\panel.html"])
async def test_folder_templates_and_manifests_are_not_served(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get(f"/static/v/{ASSET_VERSION}/{path}")).status_code == 404
        assert (await ac.get(f"/static/{path}")).status_code == 404
        assert (await ac.get(f"/static/v/{ASSET_VERSION}/panels/watchlist/panel.js")).status_code == 200


# --- every hook the markup names exists -----------------------------------------------------------------------------

def _registered() -> dict:
    found = {"call": set(), "action": set(), "change": set()}
    for path in all_js():
        body = text(path)
        for block in re.findall(r"\bregister\(\{(.*?)\}\);", body, re.S):
            found["call"] |= set(re.findall(r"(?:^|[{,])\s*(\w+)\s*(?=[,:]|$)", block.strip(), re.M))
        found["action"] |= set(re.findall(r"""registerAction\(\s*["']([\w-]+)["']""", body))
        found["change"] |= set(re.findall(r"""registerChange\(\s*["']([\w-]+)["']""", body))
    return found


@pytest.mark.asyncio
async def test_every_delegated_action_in_the_markup_is_registered():
    registered = _registered()
    assert {"openShow", "openAddModal", "openReview", "openLibrary", "startReconnect", "markNext"} <= registered["call"]
    # (a module's comments may name an attribute as an example: "data-call="name"")
    sources = {"page": await rendered_page(), **{rel(p): re.sub(r"\s//\s.*$", "", text(p), flags=re.M) for p in all_js()}}
    used_any = {"call": set(), "action": set(), "change": set()}
    for name, body in sources.items():
        for kind in used_any:
            used = set(re.findall(rf"""data-{kind}=["']([\w-]+)["']""", body))
            used |= set(re.findall(rf"""\.dataset\.{kind}\s*=\s*["']([\w-]+)["']""", body))
            used_any[kind] |= used
            assert used <= registered[kind], f"{name}: no {kind} registered for {sorted(used - registered[kind])}"
    assert len(used_any["call"]) >= 18 and len(used_any["action"]) >= 8  # the scan sees the hooks


def test_registering_a_name_twice_is_avoided():
    seen: dict = {}
    for path in all_js():
        body = text(path)
        names = [("action", n) for n in re.findall(r"""registerAction\(\s*["']([\w-]+)["']""", body)]
        names += [("change", n) for n in re.findall(r"""registerChange\(\s*["']([\w-]+)["']""", body)]
        for block in re.findall(r"\bregister\(\{(.*?)\}\);", body, re.S):
            names += [("call", n) for n in re.findall(r"(?:^|[{,])\s*(\w+)\s*(?=[,:]|$)", block.strip(), re.M)]
        for key in names:
            other = seen.setdefault(key, rel(path))
            assert other == rel(path), f"{key} is registered by {other} and {rel(path)}"


@pytest.mark.asyncio
async def test_every_getelementbyid_literal_exists():
    """A renamed or misspelled id makes getElementById return null and the handler throws."""
    html = await rendered_page()
    known = set(re.findall(r"""\bid=["']([\w-]+)["']""", html))
    lookups: dict = {}
    for path in all_js():
        body = text(path)
        known |= set(re.findall(r"""\bid=\\?["']([\w-]+)\\?["']""", body))  # ids the module builds
        known |= set(re.findall(r"""\.id\s*=\s*["']([\w-]+)["']""", body))
        for name in re.findall(r"""(?:getElementById|openModal|closeModal|isOpen|onModalClose)\(\s*["']([\w-]+)["']""", body):
            lookups.setdefault(name, set()).add(rel(path))
        for name in re.findall(r"""querySelector(?:All)?\(\s*["']#([\w-]+)""", body):
            lookups.setdefault(name, set()).add(rel(path))
    assert len(lookups) > 100
    missing = {name: sorted(files) for name, files in lookups.items() if name not in known}
    assert not missing, f"ids that exist nowhere: {missing}"


def _first_argument(body: str, start: int) -> str:
    depth, i = 0, start
    while i < len(body):
        c = body[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return body[start:i]


def _icon_names(body: str) -> set:
    """The sprite names a module draws: icon("x"), icon(cond ? "x" : "y") and the tables that feed icon(...)."""
    names = set()
    for m in re.finditer(r"\bicon\(", body):
        arg = _first_argument(body, m.end()).strip()
        literal = re.fullmatch(r"""["']([\w-]+)["']""", arg)
        names |= {literal.group(1)} if literal else set(re.findall(r"""\?\s*["']([\w-]+)["']\s*:\s*["']([\w-]+)["']""", arg) and
                                                         sum(re.findall(r"""\?\s*["']([\w-]+)["']\s*:\s*["']([\w-]+)["']""", arg), ()))
    names |= set(re.findall(r"""\bicon:\s*["']([\w-]+)["']""", body))                 # { icon: "x", label, cls }
    names |= set(re.findall(r"""\[\s*"(?:st-\w+)?",\s*"([\w-]+)",""", body))           # [status class, icon, word]
    names |= set(re.findall(r"""\[\s*"([\w-]+)",\s*"st-\w+"\s*\]""", body))            # [icon, status class]
    names |= set(re.findall(r"""\w+:\s*\[\s*"([\w-]+)",\s*"[A-Z][^"]*"\s*\]""", body))  # media: [icon, word]
    names |= set(re.findall(r"""(?:movie|anime|show):\s*"([a-z][\w-]*)"[,\s}]""", body))  # media: icon
    names |= set(re.findall(r"""href="#i-([\w-]+)""", body))
    return names


@pytest.mark.asyncio
async def test_every_sprite_icon_used_exists():
    sprite = set(re.findall(r'<symbol id="i-([\w-]+)"', text(WEB / "templates" / "icons.svg")))
    used = set(re.findall(r'href="#i-([\w-]+)"', await rendered_page()))
    for path in all_js():
        used |= _icon_names(text(path))
    assert len(used) > 50, sorted(used)  # the scan sees the icons
    assert {"film", "sparkle", "tv", "plug", "down", "pause", "arrow-up"} <= used
    assert used - sprite == set()


EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u25A0-\u25FF\uFE0F]")


@pytest.mark.parametrize("path", [*all_html(), *all_js(), *all_css()], ids=rel)
def test_no_emoji_or_symbol_glyphs_in_the_ui(path):
    """Icons are the sprite's line icons (or plain words); titles from SIMKL keep whatever they have."""
    assert EMOJI.findall(text(path)) == []


def test_no_module_shares_window_globals():
    """The modules share nothing through window globals (the old system.js / insights.js bridge is gone)."""
    for path in all_js():
        assert "Object.assign(window" not in text(path) and "window.load" not in text(path), rel(path)


def test_the_schedule_loads_when_its_tab_opens_and_its_posters_are_lazy():
    js = text(STATIC / "panels" / "calendar" / "panel.js")
    assert 'class="poster timeline-thumb"' in js and 'loading="lazy"' in js.split('class="poster timeline-thumb"')[1].split(">")[0]
    assert 'onTabActivated("calendar-tab", () => fetchUpcomingCalendar())' in js
    assert "registerLoader(fetchUpcomingCalendar)" not in js  # not fetched with the page
    assert "opened ? fetchUpcomingCalendar() : undefined" in js  # refreshed after a sync only once it has been opened
