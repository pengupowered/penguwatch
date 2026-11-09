# Themes

A theme is a palette (it sets PenguWatch's colours) and, for some themes, a skin (shapes, glows and textures) and a subtle
animated effect behind the page. Every browser picks one in the header's theme menu (the palette button), and the server's
`display.theme` is the one a browser starts with. This page is the reference for adding a theme: a theme is one folder,
and nothing else in the code names it.

- [Picking a theme](#picking-a-theme)
- [The server default](#the-server-default)
- [The folder](#the-folder)
- [theme.json](#themejson)
- [The tokens](#the-tokens)
- [Skins](#skins) and the [page hooks a skin may rely on](#page-hooks-a-skin-may-rely-on)
- [Effects](#effects) and [measuring one](#measuring-an-effect)
- [Checks every theme must pass](#checks-every-theme-must-pass)
- [How the page applies a theme](#how-the-page-applies-a-theme)
- [Adding a theme](#adding-a-theme) and [the contract for a skin author](#the-contract-for-a-skin-author)
- [Palette credits](#palette-credits)

The theme engine and the first seven palettes are copied from the maintainer's other dashboard project and owned by PenguWatch from
here on (nothing is shared at run time). Skins and effects follow the same design; that project's per-card views, chart
styles and custom themes are not part of PenguWatch.

## Picking a theme

The palette button in the header opens a menu with two groups and a switch.

- **Theme for this browser**: `Use the default` (the server's default, see below), then two groups, each by name.
  **Palettes** (colours only): `PenguWatch` (the standard look), `Blueprint`, `Catppuccin`, `Dracula`, `E-ink`,
  `Gruvbox`, `High contrast`, `Nord`, `One Dark`, `Rosé Pine`, `Solarized` and `Tokyo Night`. **Skins** (colours, shapes
  and a subtle animated effect): `Amber CRT` (the page inside an old amber monitor: a rounded bezel, monospace
  throughout, a status line, soft-key tabs F1 to F12, prompt titles with a block cursor, double-ruled cards, [ BRACKETED ]
  buttons, inverse-video chips, a slow roll down the glass); `Cinema` (a movie palace: velvet curtains at the page's
  edges, the header a marquee sign framed in bulbs over a scalloped valance, ticket-stub tabs, page titles on a letter
  board, gilt poster-case cards, programme-framed panels, plaque buttons; the bulbs brighten in turn and a warm light
  drifts over the curtains); `Starship console` (a starship computer console: an elbow frame whose rail down the left side holds the tabs as
  rounded colour blocks, panels and dialogs framed by bars, bracketed cards, condensed capitals on black; two blocks at
  the foot of the rail brighten in turn and a faint sweep passes behind the header); `Synthwave` (1980s neon on deep purple: chrome
  italic titles over a striped sunset bar, neon-tube tabs, slanted buttons and chips, square neon-edged cards with angled
  header strips, and a striped sun at the bottom of the window over a slow grid floor); `Video store` (a rental shop in red, teal and
  cream: a shop-front header with a lit sign, a scalloped striped awning and the tabs hanging from it as aisle signs,
  label-tape titles, tape-box cards with a labelled spine, price-sticker chips, chunky buttons, an "after hours" dark
  mode, a faint tracking line in the header while the open tab's sign sways).
- **Light or dark**: `Like the system`, `Light` or `Dark`. This is a separate choice from the theme: a theme with both
  modes has a light and a dark palette, and with `Like the system` it follows the operating system
  (`prefers-color-scheme`). A theme with one mode (Amber CRT, Blueprint, Cinema, Dracula, Starship console and Synthwave
  are dark, E-ink is light) forces it: the menu says so and the light/dark choice is off.
- **Animation: on / off**: whether the theme's effect runs in this browser. Only some themes have one (Amber CRT,
  Cinema, Starship console, Synthwave and Video store); for the others the menu says the theme has no animation. It is
  never shown while the system asks for reduced motion (`prefers-reduced-motion: reduce`), whatever this says; the menu
  says so.

The menu is a normal pop-up menu: Escape or a click elsewhere closes it, and its items are buttons you can reach with Tab and
activate with Enter or Space. The choices are kept in this browser only (`localStorage`: `penguwatch.look` for the theme
id, `penguwatch.mode` for `light` or `dark`, `penguwatch.effects` for `on` or `off`); nothing is sent to the server. With
no animation choice of its own a browser follows the **Theme animation** setting (`display.effects`, Settings > Display,
on by default).

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
  theme.json   names the theme and gives its tokens (every theme)
  skin.css     optional: shapes, glows and textures, every rule scoped to the theme (Skins, below)
  effect.js    optional: a subtle animation behind the page (Effects, below)
```

Nothing else may be in the folder (a `theme.css` there would be hidden by the generated one); the loader leaves a folder
with another file out. The `default` theme is `tokens.css`: it has neither a skin nor an effect.

The id is the folder's name: lower-case letters, digits and dashes, at most 40 characters. At start,
`penguwatch.web.themes` reads every folder; the page and `GET /api/themes` list the themes in this order: `default` first,
then by name. A folder whose `theme.json` breaks a rule is left out and logged, and `tests/test_themes.py` fails on it.

Names are generic: describe the look, never a film, series, game or brand, and do not copy logos, fonts or trade dress. A
published palette under a free licence (Nord, Dracula, Solarized, Catppuccin, Gruvbox, Rosé Pine, One Dark; MIT, Tokyo Night; Apache-2.0)
may keep its name; say so and give its source in the description, and in [Palette credits](#palette-credits). Themes
use the system's fonts: a palette cannot change fonts, sizes or shapes (a skin may change shapes, see below).

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

## Skins

`skin.css` changes shapes and textures: borders, glows, letter case, corner radii, background gradients. It is linked
only while its theme is in use, after the theme's token blocks, and the same rules as for the page's own stylesheets apply
(colours through tokens, 390 px without sideways scrolling, the tab strip fitting from 1024 to 1920 px). The rules, held by
`penguwatch.web.themes.skin_problems` at load (a theme whose skin breaks one is left out and logged) and by
`tests/test_themes.py`:

- **Scoped.** Every selector starts with `:root[data-look="<id>"]` (the attribute the boot script sets), in every list
  and inside `@media`, `@supports` and `@container` too, so a skin never touches another theme or the page without it.
  `:root[data-look="synthwave"] :is(.a, .b)` is one selector; `:root[data-look="synthwave"] .a, .b` is refused for `.b`.
- **No nested rules**, no at-rule but `@media`, `@supports`, `@container` and `@keyframes`, and a keyframes name starts
  with `<id>-` (names are global).
- **Colours from the tokens only**: `var(--accent)`, `color-mix(in srgb, var(--accent) 40%, transparent)`. No hex, `rgb()`,
  `hsl()` or named colour (`transparent` and `currentColor` are fine). A skin may define its own custom properties
  (`--sw-glow`), but never a palette token (those are `theme.json`'s, held by the checks) or a fixed one (`--on-image*`,
  `--poster-ratio`, `--tabs-h`).
- **No `url()`, `@import`, `@font-face`**, `expression()`, `javascript:` or backslash escapes: themes use the system's
  fonts, and draw with gradients, borders and shadows.
- **The effect layer stays put.** A skin may style what an effect draws in `.fx-layer` (its canvases), but may not set
  `z-index` or `pointer-events` there, nor place the layer itself (`position`, `inset`, `transform`, `display`,
  `opacity` ...).
- **Leave posters alone.** Do not restyle `.poster`, `.poster-fallback` or the `.on-image` chips over them: the image
  decides the ground, and `--on-image*` are tuned for every image. A card's edge and glow sit around the poster, never on it.
- **Keep the tab labels' font and size** (`.nav-btn`): the tab strip measures them to choose full labels, short labels or
  icons, and the test needs every tab inside the strip from 1024 to 1920 px and the full labels at 1920.
- **Motion** in a skin (an animation or a transition beyond the page's own) stops under
  `@media (prefers-reduced-motion: reduce)`; anything that moves all the time belongs in an effect.

### Page hooks a skin may rely on

A skin styles the page's own parts by these classes. The page template, the tabs' markup and their scripts keep them, and
`tests/test_themes.py` fails when one disappears (`test_the_page_keeps_the_hooks_a_skin_relies_on`: each class is still
written by the file that makes it and still has a rule in the page's stylesheets; `..._the_structure_...`: the nesting
below), so a refactor cannot quietly break a skin. Add a hook here and to `PAGE_HOOKS` in that test before a skin relies on
something new.

| Part | Hooks |
|---|---|
| The effect layer | `.fx-layer` (`#fx-layer`), the first element of `<body>`: fixed, behind everything, never hit. An effect draws in it; a skin may style what is drawn there |
| The header | `header.topbar` (its `::before` is the header's see-through background and bottom border), with `.tb-inner` its first child: `.brand-name`, `.status-pill`, `.tb-actions` (the header's buttons) |
| The brand | `.brand`, the header's first link: the mark (`img.brand-mark`) and `.brand-name` |
| The tab bar | `nav.tabs`, the header's second child, holding `.tabs-inner` with one `button.nav-btn[role=tab]` per tab (`.active` on the open one; its `::after` is the indicator on wide screens, its `::before` on the phone's bottom bar), then `.nav-more` (phones) |
| A tab's page | `main.app-main` holding one `section.tab-pane[data-panel]` per tab (`.active` on the open one), each starting with its `.page-h` (`svg.i`, `.page-t` the title, `.page-m`) |
| Cards | `.upnext-card` (Up Next), `.show-card` (Library), `.discover-card` (Discover), `.timeline-card` (Air Schedule); their edge is `--card-line` |
| Panels | `.panel` with `.panel-h` (`.panel-t`), `.panel-b` and, where it has one, `.panel-f` (Services, Optimizer, History's tiles, Settings' sections ...) |
| Chips | `.chip`; `button.chip[aria-pressed]` (filters), `.chip.st` (a status: leave its colours to `--st`) |
| Buttons | `.btn` (`.primary`, `.outline`, `.danger`, `.icon`, `.sm`), `.seg` with `button[aria-pressed]` (segmented choices) |
| Inputs | `.input` (inputs, selects and text areas) |
| Dialogs | `.modal` (the backdrop) holding `.modal-content`, with `.modal-h` (`.modal-t`), `.modal-b` and `.modal-f` |
| Menus | `.menu`: the header's pop-up menus (the theme menu, the Grafana links) |
| Notes | `.empty` (an empty or error state) and `.result` (a note or the outcome of an action, coloured by `--st`) |
| The Settings rows | `.set-sec` (a section, also a `.panel`), `.set-row` (one setting), `.set-card` (a webhook) |
| The Settings bar | `.set-bar` (Versions, Export, Import, Cancel and Save, sticky at the bottom of the window) |
| History's charts | `svg.hc-svg` with `.hc-grid` (grid lines), `.hc-axis` (labels) and `path.hc-bar` (bars) |
| Posters (left alone) | `.poster`, `.poster-fallback`, `.on-image` chips: hooks for the tests, not for restyling |

## Effects

`effect.js` is an ES module with one export:

```js
export default {
  start(layer, ctx) {
    const canvas = document.createElement("canvas");
    layer.append(canvas);
    const g = canvas.getContext("2d");
    const off = ctx.onFrame((now, dt) => {
      g.fillStyle = ctx.tokens()["--accent"]; // read in each frame: it follows a light/dark switch
      // ... draw one frame
    }, 4); // frames a second this effect needs (at most ctx.fps)
    return () => { off(); canvas.remove(); }; // stop: undo everything start did
  },
};
```

- `layer` is `#fx-layer`: a fixed element over the window, **behind every part of the page** (`z-index: -1` in the root's
  stacking context: above the page's background, under the header, the cards and the text), with `pointer-events: none`,
  `aria-hidden`. Draw into it only. A card is opaque, so an effect shows in the page's background: between and around the
  cards, and through the see-through header.
- `ctx.tokens()` gives the current tokens by name (`ctx.tokens()["--accent"]`), read when asked. Colours come from there
  only: no colour literal in an effect.
- `ctx.fps` is the engine's cap, `MAX_FPS` (12). `ctx.onFrame(cb, fps)` registers `cb(now, dt)` with the engine's one frame
  loop and returns a function that unregisters it. Ask for as few frames as the look allows: every frame costs the page a
  composite pass, whatever the effect draws.
- `start` returns `stop`; it removes what it added and every listener it set (a `resize` handler, say). The engine also
  empties the layer when it stops an effect.
- What an effect does not do (`penguwatch.web.themes.effect_problems`, at load and in the tests): import anything, keep
  its own time (`requestAnimationFrame`, `setInterval`, `setTimeout`), fetch or store anything, build code from strings
  (`eval`, `Function`), write HTML, reach outside its layer (`document.body`, `querySelector`, `getElementById`), set
  `zIndex` or `pointerEvents`, or read `matchMedia`. The page's CSP (`script-src 'self'`) allows the module itself and
  nothing inline.

**The engine owns the limits** (`static/js/themes.js`), not the effect. It runs an effect only while all of these hold, and
stops or pauses it when one changes:

| Limit | What the engine does |
|---|---|
| Reduced motion | never started while the system asks for `prefers-reduced-motion: reduce`; stopped when it starts asking |
| A hidden tab | paused while `document.hidden` (no frame, no timer), resumed when the tab is shown |
| Frame rate | at most `MAX_FPS` (12) frames a second, or the fewer the effect asks for |
| Frame budget | the effect's own drawing time per frame is averaged; over 4 ms the rate halves, down to 1 frame a second; past that the effect is stopped for the rest of the page's life (until Animation is switched off and on) |
| Behind the content | the effect gets `#fx-layer` only; page.css keeps it behind the page and out of the pointer's way, a skin may not move it, and the UI test hit-tests every poster's and chip's centre with the layer made hittable on purpose: it must never be the top element there |
| The setting | runs only while effects are on: this browser's **Animation** choice in the theme menu, else **Theme animation** (`display.effects`) |
| Only our files | the effect's URL must be `/static/v/<version>/themes/<id>/effect.js` (`ourEffect` in `js/themes.js`; `tests/test_themes.py` refuses foreign ones) |

The layer tells what runs, for the tests and for anyone looking: `data-fx` (the theme id), `data-state` (`running`,
`paused`, or `over-budget` once given up) and `data-fps` (the rate now); its `fxFrames` property counts the frames drawn.

### Measuring an effect

Budget: **at most about 3 % more main-thread time** than the same page without the effect, in the headless Chromium of the
UI test image at 1440 x 1000 on the demo library. Measure with Chrome's `Performance.getMetrics` (`TaskDuration`, the main
thread's busy time): load the page with the theme picked and reduced motion off, wait for it to settle, then take 30 s with
the effect on and 30 s with it off (the theme menu's Animation item), on Up Next and on History, and compare the busy time
per second of wall time.

Measured for 0.17 (two rounds of 30 s each way, the average; the busy time of the main thread as a share of wall time):

| Effect | Tab | Effect off | Effect on | Added |
|---|---|---|---|---|
| Amber CRT, 6 fps, one band a quarter of the window high on a canvas one pixel wide, moved by a transform; the title cursor's blink (default theme: 0.17 % and 0.14 %) | Up Next | 0.17 % | 1.44 % | +1.3 % |
| | History | 0.20 % | 1.54 % | +1.3 % |
| Cinema, 3 fps, two canvases of a curtain's width (46 px at 1440), plus the skin's gated bulb fade (CSS opacity) | Up Next | 0.15 % | 1.16 % | +1.0 % |
| | History | 0.15 % | 1.17 % | +1.0 % |
| Starship console, 3 fps, one canvas of the header's height (74 px at 1440), and two rail blocks fading in turn (CSS opacity) | Up Next | 0.17 % | 1.06 % | +0.9 % |
| | History | 0.16 % | 1.02 % | +0.9 % |
| Synthwave, 4 fps, two canvases of the floor's height at the bottom of the window (260 px at 1440); the standard theme beside it: 0.19 % and 0.18 % | Up Next | 0.19 % | 0.97 % | +0.8 % |
| | History | 0.18 % | 0.85 % | +0.7 % |
| Video store, 3 fps, one canvas of the header's height (130 px at 1440), and the open tab's sign swaying (a CSS transform behind the `#fx-layer` gate) | Up Next | 0.14 % | 0.86 % | +0.7 % |
| | History | 0.18 % | 0.94 % | +0.8 % |

Of an effect's share, its own script is the smaller part; the rest is the
page's composite and paint for each frame, so the frame rate is what an effect's cost follows.

What keeps an effect cheap: few frames a second; canvases only as large as they must be (Synthwave's are the floor's height, not
the whole window); the still part drawn once on a canvas of its own, only the moving part each frame; each canvas on a
compositing layer of its own (`will-change: transform` in the skin), so a frame repaints the canvas and not the page; a
fade drawn into the canvas (`destination-out`) instead of a CSS mask over it.

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
stylesheet; for a theme that has them, the URLs of its skin and effect), the server default on `<html data-look-default>`
and the effects setting on `<html data-effects-default>` (`on` or `off`). `static/theme.js`, a classic script in `<head>` after the
shared stylesheets, runs before the first paint:

1. migrates the old `penguwatch.theme` key (above);
2. picks the theme: this browser's `penguwatch.look`, else the server default, else `default`;
3. sets `<html data-look="<id>">` and `<html data-theme="light|dark">` (the stored mode, or the theme's only mode; none for
   the system's);
4. for a theme with a stylesheet, writes its `<link>` where the script stands, and after it the skin's, so the browser waits
   for both before painting. It never runs the effect: `js/themes.js` starts it once the page's modules run.

`tokens.css` reads `data-theme`; the theme's stylesheet is token blocks scoped to `:root[data-look="<id>"]`, generated from
`theme.json` by `penguwatch.web.themes.look_css` and served at `/static/v/<tree>/themes/<id>/theme.css` (the service worker
caches them with the rest of the shell, skins and effects too). A skin is served as it is, at
`/static/v/<tree>/themes/<id>/skin.css`, and an effect at `/static/v/<tree>/themes/<id>/effect.js`. Only the theme in use is
linked, and only its effect runs.

**The boot script links only our own files.** A theme's stylesheet or skin URL is linked only when it matches
`/static/v/<version>/themes/<id>/<name>.css`; anything else in the list is ignored. `tests/test_themes.py` holds every URL the
server lists to that pattern and refuses a set of foreign ones (another host, `//`, `javascript:`, a path that climbs out, a
different folder, a script, a query), so loosening the pattern fails the test. The header menu applies the same rule when it
links a stylesheet on a switch, and a second test holds the two patterns equal. An effect is imported only from
`/static/v/<version>/themes/<id>/effect.js` (`ourEffect`), held to its own list of foreign URLs.

When a theme is picked in the menu, `js/themes.js` links the new theme's stylesheet and skin and changes `data-look` only after
both have loaded, so there is no moment in the new theme without its colours or shapes; then it unlinks the old ones, stops
the old theme's effect and starts the new one's. It also keeps `<meta name="theme-color">` in step with `--bg` for the
phone's browser bar.

## Adding a theme

1. Make the folder `src/penguwatch/web/static/themes/<id>/` with a `theme.json`: copy `nord`'s and change the id, name,
   description and colours. Give every colour token in each mode (the loader names the ones that are missing).
2. Run `scripts/test.sh -k themes`. The folder is picked up by `test_every_theme_folder`; it names any check the palette
   fails, with the ratio. Adjust the palette until it passes.
3. Run `scripts/test-ui.sh` (`THEMES` in `tests/ui/lib.mjs` is read from the theme folders): it switches to the theme in each of
   its modes at 1440 and 390 px and compares the colours the page computes with the file.
4. Add the id to the list in `docs/configuration.md` and `config.example.yaml` (both copies), to `IDS` in
   `tests/test_themes.py`, the name to the credits below if it is a published palette, and a line to the CHANGELOG.

Nothing else names a theme. The page and the menu list whatever folders there are.

### The contract for a skin author

Synthwave (`static/themes/synthwave/`) is the reference: copy its folder and change it.

1. **Files.** `theme.json` (the palette, held to the checks above; a skin is often dark only, as Synthwave is), `skin.css`,
   and optionally `effect.js`. Nothing else in the folder.
2. **Scoping.** Every rule of `skin.css` under `:root[data-look="<id>"]`; colours from the tokens only; no palette or fixed
   token set; no `url()`, `@import`, `@font-face` or nesting; keyframes named `<id>-...` ([Skins](#skins)).
3. **Hooks.** Style only the [page hooks](#page-hooks-a-skin-may-rely-on); need another, add it to the table and to
   `PAGE_HOOKS` in `tests/test_themes.py` in the same change. Leave posters and their `.on-image` chips alone, and the tab
   labels' font and size.
4. **Effect.** `export default { start(layer, ctx) { ...; return stop; } }`, drawing only into `layer` with `ctx.onFrame` at
   the fewest frames the look allows, colours from `ctx.tokens()`, none of the forbidden calls ([Effects](#effects)).
   Subtle: it sits behind the page and must not draw the eye from the content.
5. **Limits.** The tab strip fits at 1024, 1280, 1366 and 1920 px (full labels at 1920); nothing sticks out at 390 px;
   posters and their chips stay readable; the effect adds at most about 3 % main-thread time
   ([Measuring an effect](#measuring-an-effect)): measure it and put the numbers in the table there.
6. **Tests.** Add the id to `IDS` and `SKINNED` in `tests/test_themes.py` (`scripts/test.sh -k themes`: the palette checks,
   the skin and effect contracts, the folder), and to `THEMES` and `SKINS` in `tests/ui/lib.mjs` with `{ effect: true }` when
   it has one (`scripts/test-ui.sh`: the theme in each of its modes at 1440 and 390 px, the tab strip, every tab at both
   widths, the skin linked and scoped, the effect started, paused while hidden, never under reduced motion, off with the
   Animation item, never above a poster, and gone on a switch). Look at every tab yourself in both widths, and add the
   name to the menu's list in this page and in `docs/configuration.md`.

## Palette credits

The colour values of the palette themes come from each project's own repository, are used under its licence, and keep its
name. A mapping to PenguWatch's tokens is ours (surfaces, ink, status and series roles), and some text colours were nudged
(next section).

| Theme | Colours from | Licence |
|---|---|---|
| Catppuccin (Mocha, Latte) | [`catppuccin/catppuccin`](https://github.com/catppuccin/catppuccin), `palette.json` of `catppuccin/palette` | MIT, Copyright (c) 2021 Catppuccin |
| Gruvbox (dark, light) | [`morhetz/gruvbox`](https://github.com/morhetz/gruvbox), `colors/gruvbox.vim` | MIT/X11 (stated in its README; the repository has no LICENSE file), by morhetz |
| Tokyo Night (Night, Day) | [`folke/tokyonight.nvim`](https://github.com/folke/tokyonight.nvim), `extras/lua/tokyonight_night.lua` and `tokyonight_day.lua` | Apache-2.0, by folke |
| Rosé Pine (Main, Dawn) | [`rose-pine/palette`](https://github.com/rose-pine/palette), `dist/css/rose-pine.css` | MIT, Copyright (c) mvllow |
| One Dark, One Light | [`atom/one-dark-syntax`](https://github.com/atom/one-dark-syntax) and [`atom/one-light-syntax`](https://github.com/atom/one-light-syntax), `styles/colors.less` (the HSL values, converted to hex) | MIT, Copyright (c) 2016 GitHub Inc. |

Notes on the mapping. Rosé Pine's palette has no green: `--good` is its foam (Main) or pine (Dawn). The Atom syntax themes give
One Dark and One Light one background and a few greys, so their surface and line steps are lightness steps of that
background's own HSL hue. Tokyo Day has no panel lighter than its background, so its panels are the background and the
page the next darker step. Gruvbox's `--good` is its aqua, which stays apart from its yellow `--warn`. The earlier palettes
(Nord, Dracula, Solarized, Blueprint) are described in their own `theme.json`.

### Colours nudged to pass the checks

A colour that failed a check was moved toward black (light modes) or white (dark modes) in sRGB, one per cent at a time, until
it passed, so its hue stays; a pair of status colours too close was moved the same way until they were 12 apart. The numbers
are the contrast ratios (WCAG) against the surface that fails worst; no check was loosened.

| Theme and mode | Token | Palette colour | Used | Result |
|---|---|---|---|---|
| Catppuccin dark | `--good` | `#a6e3a1` | `#a1dc9c` | kept 7.77:1 as text; moved warn apart (12 needed) |
| Catppuccin light | `--ink-2` | `#5c5f77` | `#56586f` | 4.05 to 4.5 (text) |
| Catppuccin light | `--ink-3` | `#6c6f85` | `#5f6275` | 3.73 to 4.55 (text) |
| Catppuccin light | `--ink-4` | `#7c7f93` | `#707284` | 2.56 to 3.07 (text) |
| Catppuccin light | `--accent` | `#8839ef` | `#8738ed` | 4.45 to 4.52 (text) |
| Catppuccin light | `--good` | `#40a02b` | `#2d701e` | 2.52 to 4.59 (text) |
| Catppuccin light | `--warn` | `#df8e1d` | `#8c5912` | 2.03 to 4.59 (text) |
| Catppuccin light | `--serious` | `#fe640b` | `#a84207` | 2.23 to 4.56 (text) |
| Catppuccin light | `--crit` | `#d20f39` | `#b70d32` | 3.67 to 4.56 (text) |
| Catppuccin light | `--busy` | `#7287fd` | `#4f5daf` | 2.43 to 4.55 (text) |
| Catppuccin light | `--unknown` | `#7c7f93` | `#5f6271` | 2.96 to 4.53 (text) |
| Gruvbox dark | `--crit` | `#fb4934` | `#fc6a59` | 3.77 to 4.54 (text) |
| Gruvbox dark | `--busy` | `#d3869b` | `#d995a8` | kept 5.1:1 as text; moved crit apart (12 needed) |
| Gruvbox dark | `--unknown` | `#a89984` | `#ab9c88` | 4.36 to 4.53 (text) |
| Gruvbox light | `--good` | `#427b58` | `#396a4c` | 3.62 to 4.56 (text) |
| Gruvbox light | `--warn` | `#b57614` | `#88580f` | 2.81 to 4.54 (text) |
| Gruvbox light | `--serious` | `#af3a03` | `#a83803` | 4.26 to 4.52 (text) |
| Gruvbox light | `--unknown` | `#7c6f64` | `#6b5f56` | 3.55 to 4.51 (text) |
| Tokyo Night dark | `--ink-3` | `#737aa2` | `#8f95b5` | 3.22 to 4.56 (text) |
| Tokyo Night dark | `--ink-4` | `#737aa2` | `#878daf` | 2.35 to 3.02 (text) |
| Tokyo Night dark | `--unknown` | `#737aa2` | `#878daf` | 3.57 to 4.58 (text) |
| Tokyo Night dark | `--warn` | `#e0af68` | `#d7a864` | kept 6.26:1 as text; moved good apart (12 needed) |
| Tokyo Night light | `--ink-1` | `#3760bf` | `#2e51a0` | 3.52 to 4.51 (text) |
| Tokyo Night light | `--ink-2` | `#6172b0` | `#46527f` | 2.78 to 4.55 (text) |
| Tokyo Night light | `--ink-3` | `#68709a` | `#545b7d` | 3.27 to 4.51 (text) |
| Tokyo Night light | `--ink-4` | `#68709a` | `#656d95` | 2.89 to 3.02 (text) |
| Tokyo Night light | `--accent` | `#2e7de9` | `#225baa` | 2.74 to 4.55 (text) |
| Tokyo Night light | `--focus` | `#2e7de9` | `#2b76db` | 2.74 to 3.03 (text) |
| Tokyo Night light | `--good` | `#118c74` | `#0a5647` | kept 5.6:1 as text; moved warn apart (12 needed) |
| Tokyo Night light | `--warn` | `#8c6c3e` | `#6f5531` | 3.14 to 4.5 (text) |
| Tokyo Night light | `--serious` | `#b15c00` | `#884700` | 3.05 to 4.56 (text) |
| Tokyo Night light | `--crit` | `#f52a65` | `#a71d45` | 2.44 to 4.54 (text) |
| Tokyo Night light | `--busy` | `#7847bd` | `#6b3fa8` | 3.83 to 4.53 (text) |
| Tokyo Night light | `--unknown` | `#68709a` | `#515778` | 3.12 to 4.57 (text) |
| Rosé Pine dark | `--ink-4` | `#6e6a86` | `#8b889e` | 2.03 to 3.05 (text) |
| Rosé Pine dark | `--unknown` | `#908caa` | `#9491ad` | 4.31 to 4.57 (text) |
| Rosé Pine light | `--ink-3` | `#797593` | `#6a6781` | 3.67 to 4.53 (text) |
| Rosé Pine light | `--ink-4` | `#9893a5` | `#898494` | 2.48 to 3.02 (text) |
| Rosé Pine light | `--accent` | `#907aa9` | `#7a6890` | 3.47 to 4.57 (text) |
| Rosé Pine light | `--warn` | `#ea9d34` | `#916120` | 1.92 to 4.57 (text) |
| Rosé Pine light | `--serious` | `#d7827e` | `#945a57` | 2.37 to 4.53 (text) |
| Rosé Pine light | `--crit` | `#b4637a` | `#6b3b48` | kept 7.14:1 as text; moved warn apart (12 needed) |
| Rosé Pine light | `--busy` | `#907aa9` | `#726086` | 3.07 to 4.56 (text) |
| Rosé Pine light | `--unknown` | `#797593` | `#67637d` | 3.51 to 4.59 (text) |
| Rosé Pine light | `--good` | `#286983` | `#21576d` | kept 6.08:1 as text; moved busy apart (12 needed) |
| One Dark / One Light dark | `--ink-3` | `#828997` | `#959ba7` | 3.6 to 4.53 (text) |
| One Dark / One Light dark | `--ink-4` | `#828997` | `#838a98` | 3.0 to 3.04 (text) |
| One Dark / One Light dark | `--crit` | `#e06c75` | `#e5848b` | 3.74 to 4.56 (text) |
| One Dark / One Light dark | `--busy` | `#c678dd` | `#cc87e1` | 3.98 to 4.53 (text) |
| One Dark / One Light dark | `--unknown` | `#828997` | `#9ba1ac` | 3.39 to 4.59 (text) |
| One Dark / One Light dark | `--good` | `#98c379` | `#90b973` | kept 4.9:1 as text; moved warn apart (12 needed) |
| One Dark / One Light light | `--ink-3` | `#696c77` | `#636670` | 4.16 to 4.55 (text) |
| One Dark / One Light light | `--accent` | `#4078f2` | `#3868d3` | 3.62 to 4.59 (text) |
| One Dark / One Light light | `--good` | `#50a14f` | `#397338` | kept 4.66:1 as text; moved warn apart (12 needed) |
| One Dark / One Light light | `--warn` | `#c18401` | `#8b5f01` | 2.61 to 4.58 (text) |
| One Dark / One Light light | `--serious` | `#986801` | `#875d01` | 3.8 to 4.56 (text) |
| One Dark / One Light light | `--crit` | `#ca1243` | `#c01140` | 4.17 to 4.52 (text) |
| One Dark / One Light light | `--unknown` | `#696c77` | `#62646f` | 4.09 to 4.59 (text) |

Series colours that were too close to their neighbour were reordered (the same colours, a different order) in Catppuccin
Mocha, Rosé Pine Main and Dawn and One Light.
