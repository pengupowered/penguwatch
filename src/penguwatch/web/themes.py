"""Themes: the page's palettes, skins and effects, discovered from their folders at start (docs/themes.md).

No card views, chart styles, custom themes or fonts.

Every theme lives in its own folder, web/static/themes/<id>/, with
  theme.json  {"id", "name", "description", "modes": ["dark"] | ["light"] | ["dark", "light"],
               "tokens": {"dark": {"--bg": "#...", ...}, "light": {...}}, "tags": [...]}
  skin.css    optional: shapes and textures, every rule scoped to :root[data-look="<id>"] (skin_problems)
  effect.js   optional: a subtle animation behind the page, an ES module {start(layer, ctx) -> stop} (effect_problems);
              js/themes.js runs it within the engine's limits (reduced motion, hidden tab, frame budget, the setting)
so adding a theme is adding a folder. The tokens are the custom properties of tokens.css; the theme `default` is
tokens.css itself (its theme.json carries no tokens).

The page applies a theme as <html data-look="<id>">, next to data-theme (light or dark). A built-in theme's token blocks
are one generated stylesheet (`look_css`, served under the tree hash at themes/<id>/theme.css); its skin.css and effect.js
are served as they are, under the same hash. The page links the stylesheets and runs the effect only while the theme is
in use (static/theme.js, js/themes.js).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
THEMES_DIR = STATIC_DIR / "themes"
TOKENS_CSS = STATIC_DIR / "tokens.css"
MODES = ("dark", "light")
DEFAULT_ID = "default"
# Every value pattern here is used with fullmatch: `$` with match() would let a trailing "\n" through.
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

# ---------- colours ----------

_HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
HEX6 = re.compile(r"#[0-9a-fA-F]{6}")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(0|1|0?\.\d+|1\.0+)\s*)?\)")


def parse_colour(text: str) -> Optional[Tuple[float, float, float, float]]:
    """(r, g, b, alpha) with r, g, b in 0..255 and alpha in 0..1, for #rgb, #rrggbb, #rrggbbaa, rgb() and rgba();
    None for anything else."""
    text = text.strip()
    m = _HEX.fullmatch(text)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a
    m = _RGB.fullmatch(text)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        if max(r, g, b) > 255:
            return None
        return r, g, b, float(m.group(4)) if m.group(4) is not None else 1.0
    return None


def mix(a: str, b: str, amount: float) -> str:
    """`a` moved `amount` (0..1) of the way to `b`, as #rrggbb (both opaque colours, mixed in sRGB like CSS color-mix)."""
    ca, cb = parse_colour(a), parse_colour(b)
    if ca is None or cb is None:
        raise ValueError(f"not colours: {a!r}, {b!r}")
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(ca[:3], cb[:3]))


# ---------- tokens.css: the names and the default theme ----------

def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _declarations(body: str) -> Dict[str, str]:
    """`--name: value;` pairs of a rule body (other properties are left out)."""
    out: Dict[str, str] = {}
    depth, start = 0, 0
    parts = []
    for i, c in enumerate(body):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ";" and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    for part in parts:
        name, sep, value = part.partition(":")
        name = name.strip()
        if sep and name.startswith("--"):
            out[name] = " ".join(value.split())
    return out


def _block(css: str, selector: str) -> str:
    """The body of the first rule whose selector is exactly `selector` (outside @media)."""
    m = re.search(r"(^|})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if not m:
        raise ValueError(f"tokens.css has no {selector} block")
    return m.group(2)


@lru_cache(maxsize=1)
def base_tokens() -> Dict[str, Dict[str, str]]:
    """The default theme's tokens per mode, from tokens.css: dark is the bare :root block, light is dark with the
    [data-theme="light"] block over it."""
    css = _strip_comments(TOKENS_CSS.read_text(encoding="utf-8"))
    dark = _declarations(_block(css, ":root"))
    light = {**dark, **_declarations(_block(css, ':root[data-theme="light"]'))}
    return {"dark": dark, "light": light}


# PenguWatch's own tokens. A palette may set the OPTIONAL ones; when it does not, `derived` makes them from the surface
# and line tokens it has set. The FIXED ones are the same in every theme: the poster image decides the colours over it, and
# the sizes are layout.
OPTIONAL = ("--card-line", "--card-line-hover", "--poster-bg", "--shadow-card", "--grid")
FIXED = ("--on-image", "--on-image-bg", "--on-image-line", "--poster-ratio", "--tabs-h")
_SHADOW = re.compile(r"none|[0-9a-z ,.()-]{1,200}")  # no ; { } < > \ or url()
_LIGHT_CARD_SHADOW = "0 1px 2px rgba(15, 23, 32, .06), 0 2px 8px rgba(15, 23, 32, .06)"


@lru_cache(maxsize=1)
def colour_tokens() -> Tuple[str, ...]:
    """The tokens every theme must give in each of its modes: those whose value in tokens.css is a colour (backgrounds,
    ink, lines, accent, focus, status and their -bg, the heat scale --q0..--q4, series --s1..--s8), except PenguWatch's
    optional and fixed ones."""
    return tuple(n for n, v in base_tokens()["dark"].items() if parse_colour(v) and n not in OPTIONAL and n not in FIXED)


def token_problem(name: str, value: Any) -> Optional[str]:
    """Why `value` cannot be the theme's `name`, in words; None when it can."""
    if not isinstance(value, str):
        return f"{name}: the value must be text"
    value = value.strip()
    if name in colour_tokens() or (name in OPTIONAL and name != "--shadow-card"):
        return None if parse_colour(value) else f"{name}: must be a colour (#1a2b3c, #1a2b3c80 or rgba(26, 43, 60, .5))"
    if name == "--shadow-card":
        return None if _SHADOW.fullmatch(value) else f"{name}: must be none or a box-shadow made of lengths and colours"
    if name in FIXED:
        return f"{name}: the same in every theme, a theme cannot change it"
    if name in base_tokens()["dark"]:
        return f"{name}: a theme sets colours only"
    return f"{name}: not a theme token"


def derived(tokens: Dict[str, str], mode: str) -> Dict[str, str]:
    """The optional tokens a palette left out, from the surface and line tokens it has (`tokens` is the palette over the
    defaults): the card edge sits a step from the hairline toward the muted ink (the repeated cards need an edge that reads
    on its own), the poster placeholder is the chip surface, a light card has a faint shadow, and there is no grid."""
    edge = tokens["--line-strong"]
    return {
        "--card-line": mix(edge, tokens["--ink-3"], .3),
        "--card-line-hover": mix(edge, tokens["--ink-3"], .6),
        "--poster-bg": tokens["--surface-3"],
        "--shadow-card": _LIGHT_CARD_SHADOW if mode == "light" else "none",
        "--grid": "rgba(0, 0, 0, 0)",
    }


# ---------- a theme ----------

@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    description: str
    modes: Tuple[str, ...]
    tokens: Dict[str, Dict[str, str]]  # mode -> the tokens it sets (for `default`: every token of tokens.css)
    tags: Tuple[str, ...] = ()
    skin: bool = False  # the folder has a skin.css (checked by skin_problems)
    effect: bool = False  # the folder has an effect.js (checked by effect_problems)

    def resolved(self, mode: str) -> Dict[str, str]:
        """Every token in `mode` as the page has it: the theme's own over the default's, with PenguWatch's optional ones
        made from the palette when it leaves them out. A mode the theme lacks gives its only one."""
        mode = mode if mode in self.modes else self.modes[0]
        own = self.tokens.get(mode, {})
        merged = {**base_tokens()[mode], **own}
        if self.id == DEFAULT_ID:
            return merged
        return {**merged, **{n: v for n, v in derived(merged, mode).items() if n not in own}}


def check_theme(raw: Any, *, folder: Optional[Path] = None) -> Tuple[Optional[Theme], List[str]]:
    """(the theme, []) or (None, [problems in words]). `folder`: the theme's folder (its id must match its name)."""
    problems: List[str] = []
    if not isinstance(raw, dict):
        return None, ["a theme is a JSON object"]
    extra = sorted(set(raw) - {"id", "name", "description", "modes", "tokens", "tags"})
    if extra:
        problems.append(f"unknown field {extra[0]!r}")
    tid = raw.get("id")
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        problems.append("id: lower-case letters, digits and dashes, at most 40")
    elif folder is not None and tid != folder.name:
        problems.append(f"id: must be the folder's name, {folder.name!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        problems.append("name: 1 to 40 characters")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 200:
        problems.append("description: at most 200 characters")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and 0 < len(t) <= 24 for t in tags) or len(tags) > 8:
        problems.append("tags: at most 8 short words")
    modes = raw.get("modes")
    if not isinstance(modes, list) or not modes or len(set(modes)) != len(modes) or not set(modes) <= set(MODES):
        problems.append('modes: ["dark"], ["light"] or ["dark", "light"]')
        modes = []
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        problems.append("tokens: an object with the tokens of each mode")
        tokens = {}
    is_default = tid == DEFAULT_ID
    if is_default:
        if tokens:
            problems.append("tokens: the default theme's tokens are tokens.css; its theme.json gives none")
    else:
        for mode in sorted(set(tokens) - set(modes)):
            problems.append(f"tokens: {mode!r} is not one of the theme's modes")
        for mode in modes:
            given = tokens.get(mode)
            if not isinstance(given, dict):
                problems.append(f"tokens.{mode}: missing")
                continue
            for n, v in given.items():
                why = token_problem(n, v)
                if why:
                    problems.append(f"tokens.{mode}: {why}")
            missing = [n for n in colour_tokens() if n not in given]
            if missing:
                problems.append(f"tokens.{mode}: missing colours {', '.join(missing)}")
    if folder is not None:
        problems.extend(folder_problems(folder))
    if problems:
        return None, problems
    if is_default:
        tokens = dict(base_tokens())

    def has(name: str) -> bool:
        return folder is not None and (folder / name).is_file()

    return Theme(id=tid, name=name.strip(), description=description, modes=tuple(m for m in MODES if m in modes),
                 tokens={m: {n: v.strip() for n, v in tokens[m].items()} for m in modes}, tags=tuple(tags),
                 skin=has(SKIN_FILE), effect=has(EFFECT_FILE)), []


# ---------- skins and effects ----------

SKIN_FILE = "skin.css"
EFFECT_FILE = "effect.js"
FOLDER_FILES = ("theme.json", SKIN_FILE, EFFECT_FILE)


def folder_problems(folder: Path) -> List[str]:
    """A theme folder holds theme.json and, optionally, skin.css and effect.js: nothing else (a theme.css there would be
    hidden by the generated one, and any other file is not part of the contract). `default` is tokens.css: no skin, no
    effect."""
    problems = [f"{p.name}: a theme folder holds only {', '.join(FOLDER_FILES)}"
                for p in sorted(folder.iterdir()) if p.name not in FOLDER_FILES or not p.is_file()]
    if folder.name == DEFAULT_ID and any((folder / n).exists() for n in (SKIN_FILE, EFFECT_FILE)):
        problems.append("the default theme is tokens.css: it has no skin or effect")
    return problems


# A colour written out (the same pattern as tests/test_frontend_rules.py): a skin and an effect take colours from the
# tokens only, so the palette checks (theme_checks) cover what they draw.
COLOUR_LITERAL = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(")
# The named colours a skin might reach for instead of a token (`transparent` and `currentColor` are not colours of their
# own and stay allowed).
_NAMED_COLOUR = re.compile(r"(?<![\w-])(?:white|black|red|green|blue|yellow|cyan|magenta|fuchsia|pink|purple|orange|"
                           r"violet|indigo|lime|aqua|teal|navy|gold|silver|gr[ae]y|hotpink|deeppink)(?![\w-])", re.IGNORECASE)
_SKIN_AT_RULES = ("media", "supports", "container")  # walked into: their rules follow the same scope
_ALLOWED_PROPS = re.compile(r"--[a-z0-9-]+")
# the effect layer (.fx-layer, #fx-layer) itself, as a selector's last part: a skin cannot place it
_LAYER_ITSELF = re.compile(r"[.#]fx-layer(?:\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?)*$")
_LAYER_PLACEMENT = ("position", "inset", "top", "right", "bottom", "left", "transform", "translate", "display", "isolation",
                    "mix-blend-mode", "contain", "opacity", "visibility")


def _split_top(text: str, sep: str) -> List[str]:
    """`text` split at `sep` outside parentheses and brackets (a selector list with :is(a, b) stays whole)."""
    out, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == sep and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


def skin_problems(theme_id: str, css: str) -> List[str]:
    """What breaks the skin contract (docs/themes.md, "Skins"), in words; [] when the skin may be loaded.

    - every selector starts with :root[data-look="<id>"], inside @media, @supports and @container too, so a skin never
      reaches another theme or the page without it;
    - @keyframes are allowed when their name starts with "<id>-" (names are global); no other at-rule (@import,
      @font-face, @layer, @property, @scope, @page ...) and no nested rules;
    - no url() (a skin draws with gradients, borders and shadows), no expression() or javascript:;
    - colours from the tokens only: no colour literal, no named colour;
    - a skin may not set a palette token (a colour token of tokens.css, PenguWatch's optional ones) or a fixed one
      (--on-image*, --poster-ratio, --tabs-h): the palette is theme.json's, which the checks hold to their rules."""
    scope = f':root[data-look="{theme_id}"]'
    problems: List[str] = []
    text = _strip_comments(css)
    if re.search(r"url\(|expression\(|javascript:|@import|\\", text, re.IGNORECASE):
        problems.append("skin.css: no url(), @import, expression(), javascript: or backslash escapes")
    for m in COLOUR_LITERAL.finditer(text):
        problems.append(f"skin.css: colour {m.group(0)!r} written out: use a token (var(--accent), color-mix(...))")
    banned = set(colour_tokens()) | set(OPTIONAL) | set(FIXED)

    def declarations(prelude: str, selectors: List[str], body: str) -> None:
        layer = [s for s in selectors if "fx-layer" in s]
        if "{" in body:
            problems.append(f"skin.css: {prelude[:60]!r}: no nested rules")
            return
        for part in _split_top(body, ";"):
            name, sep, value = part.partition(":")
            name = name.strip()
            if not sep:
                if part.strip():
                    problems.append(f"skin.css: {part.strip()[:40]!r} is not a declaration")
                continue
            if name.startswith("--") and (name in banned or not _ALLOWED_PROPS.fullmatch(name)):
                problems.append(f"skin.css: {name} is the palette's (theme.json) or fixed; a skin cannot set it")
            # the effect layer stays behind the page and out of the pointer's way: a skin may style what the effect draws
            # in it, never move the layer itself or let anything in it be hit
            if layer and (name.lower() in ("pointer-events", "z-index") or (
                    name.lower() in _LAYER_PLACEMENT and any(_LAYER_ITSELF.search(s) for s in layer))):
                problems.append(f"skin.css: {name} on the effect layer: it stays behind the page, never hit")
            for m in _NAMED_COLOUR.finditer(value):
                problems.append(f"skin.css: colour {m.group(0)!r} written out in {name}: use a token")

    def walk(body: str, where: str) -> None:
        i = 0
        while i < len(body):
            brace = body.find("{", i)
            close = body.find("}", i)
            if close >= 0 and (brace < 0 or close < brace):
                problems.append(f"skin.css: an unmatched }} {where}")
                return
            if brace < 0:
                if body[i:].strip():
                    problems.append(f"skin.css: stray text {body[i:].strip()[:40]!r}")
                return
            prelude = body[i:brace].strip()
            depth, j = 1, brace + 1
            while j < len(body) and depth:
                depth += {"{": 1, "}": -1}.get(body[j], 0)
                j += 1
            if depth:
                problems.append(f"skin.css: {prelude[:60]!r} is not closed")
                return
            inner = body[brace + 1:j - 1]
            if prelude.startswith("@"):
                at = re.match(r"@([\w-]+)\s*(.*)", prelude, re.S)
                kind, rest = (at.group(1).lower(), at.group(2).strip()) if at else ("", "")
                if kind in _SKIN_AT_RULES:
                    walk(inner, f"in @{kind}")
                elif kind in ("keyframes", "-webkit-keyframes"):
                    if not re.fullmatch(re.escape(theme_id) + r"-[\w-]+", rest):
                        problems.append(f"skin.css: @keyframes {rest!r}: the name starts with {theme_id + '-'!r}")
                else:
                    problems.append(f"skin.css: @{kind} is not allowed (only @media, @supports, @container, @keyframes)")
            else:
                selectors = [s.strip() for s in _split_top(prelude, ",")]
                for selector in selectors:
                    if not selector.startswith(scope):
                        problems.append(f"skin.css: {selector[:60]!r} is not under {scope}")
                declarations(prelude, selectors, inner)
            i = j

    walk(text, "at the top")
    return problems


# What an effect does not do: the engine keeps time (one capped frame loop, ctx.onFrame), stores the settings and decides
# where the effect draws (the layer it is given, behind the page). No network, no code from strings.
EFFECT_FORBIDDEN = ("requestAnimationFrame", "setInterval", "setTimeout", "eval(", "Function(", "fetch(", "XMLHttpRequest",
                    "WebSocket", "EventSource", "sendBeacon", "localStorage", "sessionStorage", "indexedDB", "document.cookie",
                    "import(", "document.write", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.body",
                    "document.documentElement", "querySelector", "getElementById", "getElementsBy", "zIndex", "z-index",
                    "pointerEvents", "matchMedia")


def effect_problems(source: str) -> List[str]:
    """What breaks the effect contract (docs/themes.md, "Effects"), in words; [] when the effect may be run: an ES module
    whose default export is {start(layer, ctx)} with no import of its own and none of EFFECT_FORBIDDEN; colours come from
    ctx.tokens()."""
    problems: List[str] = []
    code = re.sub(r"/\*.*?\*/|(?<![:\\])//[^\n]*", "", source, flags=re.S)  # comments may name what the code may not use
    if not re.search(r"export\s+default\s*\{[\s\S]*\bstart\s*\(\s*\w+\s*,\s*\w+\s*\)", code):
        problems.append("effect.js: the default export is {start(layer, ctx)}, which returns stop()")
    if re.search(r"^\s*import\b", code, re.M):
        problems.append("effect.js: no import (an effect is one file)")
    for word in EFFECT_FORBIDDEN:
        if word in code:
            problems.append(f"effect.js: does not use {word.rstrip('(')} (the engine keeps time, stores and places the layer)")
    for m in COLOUR_LITERAL.finditer(code):
        problems.append(f"effect.js: colour {m.group(0)!r} written out: read it from ctx.tokens()")
    return problems


# ---------- discovery ----------

@lru_cache(maxsize=1)
def builtin_themes() -> Tuple[Theme, ...]:
    """The theme folders, `default` first, then by name. A folder with a broken theme.json is left out (logged);
    tests/test_themes.py fails on it."""
    found: List[Theme] = []
    for folder in sorted(p for p in THEMES_DIR.iterdir() if p.is_dir()) if THEMES_DIR.is_dir() else []:
        try:
            raw = json.loads((folder / "theme.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error(f"Theme {folder.name}: theme.json could not be read ({e})")
            continue
        theme, problems = check_theme(raw, folder=folder)
        if theme is not None and theme.skin:
            problems = skin_problems(theme.id, (folder / SKIN_FILE).read_text(encoding="utf-8"))
        if theme is not None and theme.effect and not problems:
            problems = effect_problems((folder / EFFECT_FILE).read_text(encoding="utf-8"))
        if problems or theme is None:
            logger.error(f"Theme {folder.name} left out: {'; '.join(problems)}")
            continue
        found.append(theme)
    found.sort(key=lambda t: (t.id != DEFAULT_ID, t.name.lower()))
    return tuple(found)


def builtin_ids() -> Tuple[str, ...]:
    return tuple(t.id for t in builtin_themes())


# ---------- CSS ----------

def _rule(selector: str, scheme: str, tokens: Dict[str, str]) -> str:
    body = "".join(f" {n}: {v};" for n, v in tokens.items())
    return f"{selector} {{ color-scheme: {scheme};{body} }}\n"


def theme_css(theme: Theme) -> str:
    """The token blocks of one theme (its palette and the optional tokens made from it). One mode: one block. Two: dark on
    the theme's selector, light where the system asks for light (unless the person picked dark) and where the person
    picked light; the more specific selectors win over tokens.css whatever the order."""
    if theme.id == DEFAULT_ID:
        return ""
    look = f':root[data-look="{theme.id}"]'

    def block(mode: str) -> Dict[str, str]:
        full = theme.resolved(mode)
        return {n: full[n] for n in (*theme.tokens[mode], *OPTIONAL) if n in full}

    if len(theme.modes) == 1:
        return _rule(look, theme.modes[0], block(theme.modes[0]))
    light = block("light")
    return (_rule(look, "dark", block("dark"))
            + "@media (prefers-color-scheme: light) {\n  "
            + _rule(f'{look}:not([data-theme="dark"])', "light", light) + "}\n"
            + _rule(f'{look}[data-theme="light"]', "light", light))


def look_css(theme: Theme) -> str:
    """Everything a built-in theme adds to the page's styles, as one stylesheet (static/theme.js links it after the page's
    shared stylesheets). "" for the default theme, which is tokens.css."""
    body = theme_css(theme)
    return f"/* generated from static/themes/{theme.id}/theme.json (penguwatch.web.themes) */\n{body}" if body else ""


@lru_cache(maxsize=1)
def look_stylesheets() -> Dict[str, str]:
    """theme id -> its stylesheet (look_css), for the themes that have one; served at /static/v/<tree>/themes/<id>/theme.css."""
    return {t.id: css for t in builtin_themes() if (css := look_css(t))}


def stylesheet_for(path: str) -> Optional[str]:
    """The generated stylesheet for a path under the versioned static root ("themes/nord/theme.css"), else None."""
    m = re.fullmatch(r"themes/([a-z0-9-]+)/theme\.css", path)
    return look_stylesheets().get(m.group(1)) if m else None


# ---------- what the page and the API get ----------

def page_list(version: str) -> List[Dict[str, Any]]:
    """The short list the page starts with (<html data-looks>): enough to apply a theme before the first paint (its
    stylesheet and skin, when it has them), to start its effect, and to name the themes in the header menu. Every URL is
    under /static/v/<version>/themes/<id>/: the boot script and the menu use nothing else (their `ours` checks)."""
    sheets = look_stylesheets()
    out = []
    for t in builtin_themes():
        entry: Dict[str, Any] = {"id": t.id, "name": t.name, "modes": list(t.modes)}
        if t.id in sheets:
            entry["css"] = f"/static/v/{version}/themes/{t.id}/theme.css"
        if t.skin:
            entry["skin"] = f"/static/v/{version}/themes/{t.id}/{SKIN_FILE}"
        if t.effect:
            entry["effect"] = f"/static/v/{version}/themes/{t.id}/{EFFECT_FILE}"
        out.append(entry)
    return out


def theme_files(version: str) -> List[str]:
    """Every theme file the page may load (the generated stylesheets, the skins, the effects), for the service worker's
    shell."""
    return [url for entry in page_list(version) for key in ("css", "skin", "effect") if (url := entry.get(key))]


def api_list(version: str) -> List[Dict[str, Any]]:
    """GET /api/themes: the page list's fields plus the description and tags."""
    by_id = {t.id: t for t in builtin_themes()}
    return [{**e, "description": by_id[e["id"]].description, "tags": list(by_id[e["id"]].tags)} for e in page_list(version)]


_warned: set = set()


def resolve_default(theme_id: str) -> str:
    """`theme_id` (config.yaml display.theme) when a theme has it, else the default theme's id, with one warning for each
    unknown id."""
    if theme_id in builtin_ids():
        return theme_id
    if theme_id not in _warned:
        _warned.add(theme_id)
        logger.warning(f"display.theme {theme_id!r} is not a theme (themes: {', '.join(builtin_ids())}); using {DEFAULT_ID!r}")
    return DEFAULT_ID
