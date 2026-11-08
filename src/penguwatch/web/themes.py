"""Themes: the page's palettes, discovered from their folders at start (docs/themes.md).

Palettes only: no skins, effects, card views,
chart styles, custom themes or fonts and sizes.

Every theme lives in its own folder, web/static/themes/<id>/, with
  theme.json  {"id", "name", "description", "modes": ["dark"] | ["light"] | ["dark", "light"],
               "tokens": {"dark": {"--bg": "#...", ...}, "light": {...}}, "tags": [...]}
so adding a theme is adding a folder. The tokens are the custom properties of tokens.css; the theme `default` is
tokens.css itself (its theme.json carries no tokens).

The page applies a theme as <html data-look="<id>">, next to data-theme (light or dark). A built-in theme's token blocks
are one generated stylesheet (`look_css`, served under the tree hash at themes/<id>/theme.css), which the page links only
while the theme is in use (static/theme.js).
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
    if problems:
        return None, problems
    if is_default:
        tokens = dict(base_tokens())
    return Theme(id=tid, name=name.strip(), description=description, modes=tuple(m for m in MODES if m in modes),
                 tokens={m: {n: v.strip() for n, v in tokens[m].items()} for m in modes}, tags=tuple(tags)), []


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
    stylesheet, when it has one) and to name the themes in the header menu."""
    sheets = look_stylesheets()
    out = []
    for t in builtin_themes():
        entry: Dict[str, Any] = {"id": t.id, "name": t.name, "modes": list(t.modes)}
        if t.id in sheets:
            entry["css"] = f"/static/v/{version}/themes/{t.id}/theme.css"
        out.append(entry)
    return out


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
