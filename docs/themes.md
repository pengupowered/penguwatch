# Themes

A theme is a palette: it sets PenguWatch's colours, and nothing else. Every browser picks one in the header's theme menu
(the palette button), and the server's `display.theme` is the one a browser starts with. This page is the reference for
adding a theme: a theme is one folder, and nothing else in the code names it.

- [Picking a theme](#picking-a-theme)
- [The server default](#the-server-default)
- [The folder](#the-folder)
- [theme.json](#themejson)
- [The tokens](#the-tokens)
- [Checks every theme must pass](#checks-every-theme-must-pass)
- [How the page applies a theme](#how-the-page-applies-a-theme)
- [Adding a theme](#adding-a-theme)

The theme engine and the seven palettes are copied from the maintainer's other dashboard project and owned by PenguWatch from
here on (nothing is shared at run time). That project's skins, effects, per-card views and custom themes are not part of
PenguWatch.

## Picking a theme

The palette button in the header opens a menu with two groups.

- **Theme for this browser**: `Use the default` (the server's default, see below), then `PenguWatch` (the standard look),
  `Nord`, `Dracula`, `Solarized`, `E-ink`, `High contrast` and `Blueprint`.
- **Light or dark**: `Like the system`, `Light` or `Dark`. This is a separate choice from the theme: a theme with both modes
  has a light and a dark palette, and with `Like the system` it follows the operating system (`prefers-color-scheme`). A
  theme with one mode (Dracula and Blueprint are dark, E-ink is light) forces it: the menu says so and the light/dark choice
  is off.

The menu is a normal pop-up menu: Escape or a click elsewhere closes it, and its items are buttons you can reach with Tab and
activate with Enter or Space. Both choices are kept in this browser only (`localStorage`: `penguwatch.look` for the theme
id, `penguwatch.mode` for `light` or `dark`); nothing is sent to the server.

A browser that used the old header button keeps its light/dark choice: the first page load reads the old
`penguwatch.theme` key once, makes its value the mode, leaves the theme unset (so the server's default applies) and removes
the old key.

The sign-in page follows the browser's light/dark choice only, not the theme. Its markup comes from the vendored
request-guard library (`homeguard`), which has neither the theme list nor the asset version a theme's stylesheet URL carries
(the boot script links nothing but `/static/v/<version>/themes/` files, see below), so it shows the standard palette in the
mode you picked (and still reads the old `penguwatch.theme` key). Following the theme there would need the guard library to
render the list and the version.

## The server default

```yaml
display:
  theme: nord
```

The **Theme for new browsers** setting (`display.theme`, Settings > Display) is the theme every browser shows until its person picks one for that browser (the pick
always wins, including `PenguWatch` picked on purpose). The value is a theme id. An id that no theme has is not an error: the
page shows `default` and the log carries one warning (`display.theme 'x' is not a theme`). An id with capitals, spaces or
other characters is refused when you save it, and the Settings tab only offers the themes that exist. `GET /api/themes` reports the id in use as `default`.

## The folder

```
src/penguwatch/web/static/themes/<id>/
  theme.json   the only file: names the theme and gives its tokens
```

The id is the folder's name: lower-case letters, digits and dashes, at most 40 characters. At start,
`penguwatch.web.themes` reads every folder; the page and `GET /api/themes` list the themes in this order: `default` first,
then by name. A folder whose `theme.json` breaks a rule is left out and logged, and `tests/test_themes.py` fails on it.

Names are generic: describe the look, never a film, series, game or brand, and do not copy logos, fonts or trade dress. A
published palette under a free licence (Nord, Dracula, Solarized; MIT) may keep its name; say so in the description. Themes
use the system's fonts: a palette cannot change fonts, sizes or shapes.

## theme.json

```json
{
  "id": "nord",
  "name": "Nord",
  "description": "The Nord palette: arctic blue-greys with frost and aurora accents. Dark and light.",
  "modes": ["dark", "light"],
  "tokens": {
    "dark": { "--bg": "#242933", "--surface-1": "#2e3440", "...": "..." },
    "light": { "--bg": "#e5e9f0", "...": "..." }
  },
  "tags": ["palette", "calm"]
}
```

| Field | What it is |
|---|---|
| `id` | the folder's name |
| `name` | shown in the theme menu; 1 to 40 characters |
| `description` | one sentence, shown by `GET /api/themes`; at most 200 characters |
| `modes` | `["dark"]`, `["light"]` or `["dark", "light"]`. A theme with one mode forces it |
| `tokens` | one object per mode in `modes`: token name to value. The `default` theme gives `{}`: its tokens are `tokens.css` |
| `tags` | up to 8 short words, for lists |

No other field is allowed.

## The tokens

The tokens are the CSS custom properties of `static/tokens.css`. Everything on the page, the SVG charts included, is drawn
from them, so a theme that sets them changes the whole page.

**Required in every mode the theme has: every colour token.**

| Group | Tokens |
|---|---|
| Backgrounds and lines | `--bg`, `--surface-1` (panels), `--surface-2` (insets), `--surface-3` (hover, chips), `--line`, `--line-strong`, `--scrim` (behind dialogs) |
| Text | `--ink-1`, `--ink-2`, `--ink-3`, `--ink-4` |
| Accent | `--accent`, `--accent-bg`, `--on-accent`, `--focus` |
| Status | `--good`, `--good-bg`, `--warn`, `--warn-bg`, `--warn-fill`, `--serious`, `--serious-bg`, `--crit`, `--crit-bg`, `--busy`, `--busy-bg`, `--unknown`, `--unknown-bg`, `--nodata` |
| Chart series | `--s1` ... `--s8`, in this order |
| Loss heat | `--q0` (none) ... `--q4` (most) |

Colours are `#rgb`, `#rrggbb`, `#rrggbbaa` or `rgb()`/`rgba()`. The status colours mean the same in every theme (good is
healthy, warn, serious, crit is an outage, busy is expected, unknown is no data yet); the page always shows a status with an
icon and a word too.

**PenguWatch's own tokens.** A palette may leave these out; the generated stylesheet then makes them from the palette's
surfaces and lines, so a theme with only the required colours already looks right.

| Token | Used for | Made from the palette when left out |
|---|---|---|
| `--card-line` | the edge of the repeated cards (Up Next, Library, Discover, Air Schedule) | `--line-strong` moved 30% toward `--ink-3` |
| `--card-line-hover` | the same, hovered | `--line-strong` moved 60% toward `--ink-3` |
| `--poster-bg` | behind a poster while it loads, and when there is none | `--surface-3` |
| `--shadow-card` | the cards' shadow: `none` or a box-shadow of lengths and colours | none in a dark mode, a faint shadow in a light one |
| `--grid` | a faint background grid (colour of its lines) | none (fully transparent) |

`--grid` is how Blueprint gets its grid-paper look without a stylesheet of its own: `components.css` draws the grid on the
body from this one token, and every other theme leaves it transparent.

**Not themeable**: `--on-image`, `--on-image-bg` and `--on-image-line` (text laid over a poster or a banner: the image
decides the colours, so they are the same in every theme), `--poster-ratio`, `--tabs-h`, and every font, size, radius, spacing,
weight, motion, shadow and layer token. A `theme.json` that sets one is refused.

## Checks every theme must pass

`penguwatch/web/theme_checks.py` is run on every theme folder in every mode by `tests/test_themes.py`; a new folder is tested
without a new test. Contrast is WCAG 2's; a translucent colour is laid over the surface it sits on first.

| Check | Rule |
|---|---|
| Text | `--ink-1` and `--ink-2` on `--bg`, `--surface-1`, `--surface-2` and `--surface-3`, and `--ink-3` on the first three: 4.5:1. `--ink-4` on `--surface-1` and `--surface-2`: 3:1 (12 px non-essential text only) |
| Links and buttons | `--accent` on `--surface-1` and `--surface-2`: 4.5:1. `--on-accent` on `--accent`: 4.5:1. `--focus` on the page and the panels: 3:1 |
| Status words | all six status colours (`--good`, `--warn`, `--serious`, `--crit`, `--busy`, `--unknown`) on `--surface-1`: 4.5:1, and each on its own `-bg` tint over `--surface-1` (a chip, a badge, a note): 4.5:1 |
| Status colours apart | `--good`, `--warn`, `--crit` and `--busy` at least 12 apart (OKLab distance, times 100) |
| Series apart | neighbouring `--s1` ... `--s8` at least 8 apart, and 6 apart under deuteranopia and protanopia |
| Over a poster | `--ink-4` on `--poster-bg` (the missing-poster icon): 3:1. `--on-image` on `--on-image-bg` over a white and a black image: 4.5:1 |
| Card edge | `--card-line` against `--bg` and `--surface-1`: 2:1 in a dark mode, 1.8:1 in a light one (which also has a shadow) |

If a palette fails, change the palette; the numbers are not loosened for a theme. When the five status colours of a published
palette did not reach 4.5:1 as text (Nord and Solarized), they were lightened or darkened until they did.

## How the page applies a theme

The server writes the themes on `<html data-looks>` (id, name, modes and, except for `default`, the URL of the theme's
stylesheet) and the server default on `<html data-look-default>`. `static/theme.js`, a classic script in `<head>` after the
shared stylesheets, runs before the first paint:

1. migrates the old `penguwatch.theme` key (above);
2. picks the theme: this browser's `penguwatch.look`, else the server default, else `default`;
3. sets `<html data-look="<id>">` and `<html data-theme="light|dark">` (the stored mode, or the theme's only mode; none for
   the system's);
4. for a theme with a stylesheet, writes its `<link>` where the script stands, so the browser waits for it before painting.

`tokens.css` reads `data-theme`; the theme's stylesheet is token blocks scoped to `:root[data-look="<id>"]`, generated from
`theme.json` by `penguwatch.web.themes.look_css` and served at `/static/v/<tree>/themes/<id>/theme.css` (the service worker
caches them with the rest of the shell). Only the theme in use is linked.

**The boot script links only our own files.** A theme's stylesheet URL is linked only when it matches
`/static/v/<version>/themes/<id>/<name>.css`; anything else in the list is ignored. `tests/test_themes.py` holds every URL the
server lists to that pattern and refuses a set of foreign ones (another host, `//`, `javascript:`, a path that climbs out, a
different folder, a script), so loosening the pattern fails the test. The header menu applies the same rule when it links a
stylesheet on a switch, and a second test holds the two patterns equal.

When a theme is picked in the menu, `js/themes.js` links the new theme's stylesheet and changes `data-look` only after it has
loaded, so there is no moment in the new theme without its colours. It also keeps `<meta name="theme-color">` in step with
`--bg` for the phone's browser bar.

## Adding a theme

1. Make the folder `src/penguwatch/web/static/themes/<id>/` with a `theme.json`: copy `nord`'s and change the id, name,
   description and colours. Give every colour token in each mode (the loader names the ones that are missing).
2. Run `scripts/test.sh -k themes`. The folder is picked up by `test_every_theme_folder`; it names any check the palette
   fails, with the ratio. Adjust the palette until it passes.
3. Add the id to `THEMES` in `tests/ui/lib.mjs` with its modes and run `scripts/test-ui.sh`: it switches to the theme in each of
   its modes at 1440 and 390 px and compares the colours the page computes with the file.
4. Add the id to the list in `docs/configuration.md` and `config.example.yaml`, and a line to the CHANGELOG.

Nothing else names a theme. The page and the menu list whatever folders there are.
