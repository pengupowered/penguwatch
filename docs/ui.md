# The web UI

The dashboard is one page served by the FastAPI app: plain HTML, CSS and ES modules with no build step, a strict CSP
(no inline script, style or handler) and line icons from one inline sprite. Its look (tokens, components, layout) belongs to PenguWatch. Tests that guard it: `scripts/test.sh` (`tests/test_frontend_rules.py` and the `tests/test_ui_*.py` files).

## Theming

Dark first. `static/tokens.css` holds every colour and size as a CSS variable; the light values apply under
`prefers-color-scheme: light` and again under `[data-theme="light"]`. That is the standard theme; six more palettes
(Nord, Dracula, Solarized, E-ink, High contrast, Blueprint) are folders of `static/themes/<id>/theme.json`, turned into
token blocks scoped to `[data-look="<id>"]` ([Themes](themes.md)). The header's theme menu (`js/themes.js`) picks a theme
and light, dark or the system's; both picks are kept in `localStorage` (`penguwatch.look`, `penguwatch.mode`), and
the Settings tab's "Theme for new browsers" (`display.theme`) is the theme a browser starts with. `static/theme.js`, a small classic script in `<head>`,
applies the picks before the page is painted and links the theme's stylesheet (only our own `/static/v/<version>/themes/`
files); `js/chrome.js` starts it and owns the header menus. The charts (Insights, System) are SVG drawn with classes, so
the tokens colour them and they follow a theme change without being redrawn. Text over a poster or a banner uses the
`--on-image*` tokens, which are the same in every theme because the image decides the background.

Posters are the app's main content: every title shows its cover, and a title without one (or one that does not load)
shows a placeholder drawn by the CSS (`--poster-bg`).

## Where things live

All under `src/penguwatch/web/`:

- `templates/index.html`: the page shell. The header (brand, the library driver's status pill, the spending figures, Add title,
  the Grafana menu when configured, the theme menu), the tabs, the reconnect banner, the More sheet, the toast.
  It includes each tab's and dialog's markup and links their stylesheets; it names none of them.
- `templates/icons.svg`: the icon sprite (24 px grid, 1.75 px stroke, round caps, `currentColor`). Use
  `<svg class="i"><use href="#i-NAME"/></svg>` in markup and `icon("NAME")` (js/util.js) in modules.
- `static/tokens.css`: design tokens (colours, type scale `--fs-*`, spacing `--sp-*`, radii, motion, layers).
- `static/components.css`: shared components (panel, badge, chip, buttons, segmented control, menu, table, stats,
  empty and loading states, forms, toast), tokens only.
- `static/page.css`: the shell (header, tab strip on wide screens, bottom tab bar and More sheet on phones).
- `static/style.css`: what several tabs share (posters and title cards, ratings and tags, the progress bar, the dialog
  as a div toggled by `.active`, review lists, stat tiles, the loading pill).
- `static/js/`: the shell and the shared library. `main.js` (entry), `tabs.js` (routes, tab activation, shortcuts),
  `mobile.js` (More sheet), `header.js` (status pill, spending, banner), `chrome.js` (menus), `themes.js` (the theme menu), `modal.js`
  (dialogs), `dispatch.js` (delegated actions), `bus.js` (publish/subscribe), `data.js` (shared loads and refresh
  after a change), `library-status.js`, `showchanges.js` (per-title changes), `api.js`, `state.js`, `util.js`.
- `static/panels/<id>/`: one folder per tab. `static/dialogs/<id>/`: the dialogs no tab owns (a title's details, Add
  a title), opened from several tabs and the header.
- `src/penguwatch/ui.py` finds the folders at start. The page is served from `/static/v/<hash>/` where the hash covers
  the whole static tree, so every module and stylesheet is cache-busted together; the service worker keeps exactly
  those URLs (`pwa.py`). A folder's `.html` and `.json` are templates and manifests: never served as files.

Event handlers are delegated (the CSP forbids inline ones): markup carries `data-call="name"` (with `data-args` as a
JSON list), `data-action`, or `data-change`, and modules register the handler with `register`, `registerAction` or
`registerChange` (`js/dispatch.js`). Markup that modules build sets sizes through `data-w` and `setWidths()` or a
style property from the script (`el.style.width`); never a `style` attribute.

## Adding a tab

A tab is one folder, `static/panels/<id>/`; no shared file names it. Files:

- `panel.json`: `{"order": 10, "title": "Up Next", "short": "Up Next", "icon": "queue", "route": "upnext",
  "key": "u", "bar": true}`. `order` places it in the tab bar, `short` is its label on a phone, `icon` a sprite name,
  `route` its address (`#/upnext`), `key` the "g then key" shortcut, `bar` puts it on the phone's bottom bar (the
  other tabs are in the More sheet).
- `panel.html`: exactly one `<section id="<id>-tab" class="tab-pane" data-panel="<id>" role="tabpanel"
  aria-labelledby="tab-<id>">`, starting with a `.page-h` title row and holding `.panel` cards. It is a Jinja
  template (the page's context: `household`, `grafana_links`). Any other `*.html` in the folder is a dialog the tab
  owns, included outside `<main>`.
- `panel.js`: default-exports `{ id: "<id>", mount() }`; `mount` runs once when the page starts. Register a refresh
  for when the tab opens with `onTabActivated("<id>-tab", fn)` (tabs.js), a first load with `registerLoader(fn)`
  (data.js), and listen on the bus for what changed elsewhere.
- `panel.css`: its styles, linked automatically; tokens only.

Tabs never import each other. They talk through the bus (`js/bus.js`): `show-updated` and `show-removed` (a title
changed or went), `titles-changed` (many did), `services-loaded`, `subscriptions-changed`, `prices-changed`,
`stats`, `plan`, `library-status`, `tidy-counts`, `changed-soon` / `changed-later` (the lazy refresh after a change),
`show-service-titles` (the Optimizer asks the Library to show one service). A tab's button in other markup is a
delegated action the owning tab registers (`data-call="openTidy"`, `"openReview"`, `"openLibrary"`).

## The rules

Enforced by `tests/test_frontend_rules.py`:

- No colour literal (hex, `rgb()`, `hsl()`) outside `tokens.css` (the `theme-color` meta is the one exception); every
  `var()` without a fallback names a declared property; both themes define the same tokens.
- Text colours pass WCAG AA (4.5:1) in both modes of the standard theme: the inks and the accent on the surfaces, a status
  colour on its own tint, a primary button's label, chips over a poster. `tests/test_themes.py` holds every other theme
  to the same and more ([Themes](themes.md#checks-every-theme-must-pass)).
- No `style` attribute in templates or in markup modules build, no `<style>` block, no inline handler or script. The
  CSP's `style-src` is `'self'` plus the hash of the sign-in page's own style block (homeguard's page; `login.css`
  gives it the dashboard's look), and has no `'unsafe-inline'`.
- Every tab folder is complete (manifest, one section with its id, a module with its id and `mount`, a stylesheet),
  no shared file names a tab, a folder imports only `static/js/` and its own files, every module is loaded, every
  named import exists.
- Every delegated action the markup uses is registered (once), every `getElementById` (and dialog) id exists, every
  sprite icon used exists.
- No emoji or symbol glyphs in the UI's templates, modules or styles: a line icon or a plain word. Titles from a catalogue
  keep whatever characters they have.

And by the design itself: a status is always an icon, a word and a colour (`.badge` / `.chip` with `st-good`,
`st-warn`, `st-crit`, `st-busy`, `st-unknown`); one primary button per view, the others outline or quiet; one panel
style (`.panel` with `.panel-h` and `.panel-b`); tabular numbers wherever digits line up; the system font and the
type scale from the tokens.

## Checking in a browser

The unit tests read the files; the page is checked in a real browser too: every tab and dialog at 1400 px and 390 px,
dark and light, with no console error, no CSP report, no sideways scroll on a phone and a visible keyboard focus.
Run it offline (the app and a headless Chromium in one container with `--network none`), on a copy of a library, and
never point it at a running deployment.

## The Settings tab

`panels/settings/` is the page over `/api/settings*` ([API](api.md#settings)); it is built from `GET /api/settings/schema`,
so no label, help text, range, unit, choice or environment-variable name is written in the page code, and a new setting
in the registry appears on the tab without a front-end change. Sections (Household, Sync, Optimizer, Notifications,
Display, Integrations, Data sources, Advanced, then "Kept elsewhere" and "About this install") are in the tab's left column, or a
"Jump to section" list on a phone; a save sends only what changed, with the revision it was read at. A `409` re-reads
the settings, keeps the other writer's value for what both changed, keeps your other edits in the draft and says which. A setting set by an environment variable shows
"From environment" and the variable's name and is read-only; a setting that applies after a restart says so until then.
A variable that is set but not valid is named on its row as ignored, and the row stays editable. Secret fields are
write-only: they show "set" and an address's host, never the value. Webhooks are a list of cards (up to ten); a webhook
imported with a `secret_env` or `secret_file` shows it read-only with **Stop using it**, and there is no field to name a
new one. Versions, Export and Import are on the bar at the bottom, with Cancel and Save. The tab shows the
no-access-token warning at its top while no token is set. It works at a phone's width and in every theme, and all its
colours and spacing come from the design tokens.
