# Development

## Running locally

```bash
pip install --require-hashes --no-deps -r requirements-dev.txt
PYTHONPATH=src pytest -q                          # unit and API tests (SIMKL is mocked with respx)
PYTHONPATH=src python -m penguwatch.main          # http://localhost:8080
```

From an installed wheel, `penguwatch` starts the app; see [Install](install.md).

Or with nothing installed but Podman: `scripts/test.sh` builds `Dockerfile.test` and runs the suite in a throwaway
container without network access. Extra arguments go to pytest.

The dashboard is plain HTML, CSS and ES modules: there is no build step. Static files are served from
`/static/v/<content hash>/...`, so a changed file always gets a new URL. How it is organised, theming, adding a tab
and the rules its tests enforce: [The web UI](ui.md).

## Browser tests

The dashboard has an in-repo browser test: headless Chromium (and one pass in Firefox) drives the real app on the made-up
demo library (`scripts/demo_library.py`; the private `.seed/` library is never used). It lives in `tests/ui/`. Nothing is installed on the host and nothing of it is a dependency of the project.

```bash
tests/ui/runner/build.sh        # once: the runner image localhost/ui-test-runner:1 (Chromium, Firefox, Node, puppeteer-core, ESLint)
scripts/test-ui.sh              # build Dockerfile.ui on it and run tests/test_ui_smoke.py, test_ui_lint.py, test_ui_steps.py
scripts/test-ui.sh -k token -x  # pytest arguments; --command <cmd...> runs a command in the container instead
```

`tests/ui/runner/` is shared byte for byte with the maintainer's other dashboard project, so both build the identical
image (change it there first). Building is the only step with network access; the tests run in a throwaway container with
`--network none`, no capabilities, a read-only root and a tmpfs `/tmp`, with a memory cap, a pids limit and a `--timeout`
(`PENGUWATCH_TEST_TIMEOUT`, `PENGUWATCH_TEST_MEMORY`). Test images are tagged per work tree and carry the label
`penguwatch.test-tree`; after a build the work tree's older images are removed (`scripts/lib-test-image.sh`). The runs go one
after another (no pytest-xdist). The three files are skipped in the normal unit suite unless `PENGUWATCH_UI_SMOKE=1`
is set, which `Dockerfile.ui` does. CI runs them in the `runner-image` and `ui` jobs.

- **Variants** (`test_ui_smoke.py`, each its own app on a free port with its own data directory): `demo` is the demo
  library with SIMKL not connected, in Chromium: every step file. `token` is the same behind `PENGUWATCH_ACCESS_TOKEN`:
  the run signs in through the sign-in page, then the core steps. `themed` is the app started with `display: {theme: nord}` (the core steps marked `variants: ["themed"]`: the server default theme). `household` and `solo` are the demo library
  with three people and with one (`scripts/demo_library.py --people 3` / `--people 1`; the steps marked
  `variants: ["household"]` or `["solo"]`). `demo-firefox` is the core steps in Firefox
  (`PENGUWATCH_BROWSER=firefox`, WebDriver BiDi; `FIREFOX_FILES` in `smoke.mjs`). There is no signed-in variant: a SIMKL
  login would make the sync worker call SIMKL five seconds after the start and the header would change state at an
  arbitrary moment; it needs the sync driver stubbed (a `tests/ui/serve.py`) first.
- The browser opens the app as `http://penguwatch-test:<port>`, a single-label name that `lib.mjs` maps to 127.0.0.1, so
  the page is not a secure context (no Clipboard API), like a person on the LAN.
- A run fails on any failed step, page error, console error, CSP violation, failed request or HTTP status 400 or more
  from the app. `KNOWN_RESPONSES` and `KNOWN_OVERFLOWS` in `lib.mjs` (empty now) list known ones, each with the reason.
- **Layout.** `tests/ui/smoke.mjs` is the runner, `lib.mjs` the shared context every step receives (`page`, `until`,
  `click`, `goTab`, `sticksOut`, `pickLook`, `pickMode`, `eq`, `ok`, ...), `guards.mjs` checks the step files before any step runs,
  `eslint.config.mjs` is the lint config (`static/js`, `panels`, `dialogs` as modules, `theme.js` as a classic script,
  and the tests themselves).
- **Step files** are `tests/ui/steps/<id>.mjs`: `core.mjs` (the page: header, every tab at 1440 and 390 px, the theme menu and every theme in each mode, icons),
  `<panel-id>.mjs` for a panel in `static/panels/<id>`, `<dialog-id>.mjs` for a dialog in `static/dialogs/<id>`. Order:
  core, the panels in page order, the dialogs alphabetically; the page is reloaded between files (desktop, no stored
  settings), so a file relies only on its own earlier steps. A failed step ends its file, not the run (`fatal` ends the run).
- **Adding a step file:** create `steps/<id>.mjs` exporting `subject` (`"core"`, `"panel:<id>"` or `"dialog:<id>"`),
  `variants` (default `["demo"]`) and `steps`, a list of `[name, async (ctx) => {...}, { variants, fatal, chromiumOnly }]`.
  A helper two files need goes in `lib.mjs`; a step file never declares a top-level helper of its own that another file or
  `lib.mjs` has (`test_ui_steps.py` and the runner both check). Choose selectors by id or `data-` attribute, read from the
  panel's HTML and JS. `chromiumOnly: "<reason>"` skips a step in Firefox (for CDP-only calls such as `emulateMediaFeatures`).
- **Firefox pass:** `PENGUWATCH_BROWSER=firefox` is set by the `demo-firefox` run only; to try a step there, add its file
  to `FIREFOX_FILES`. `PENGUWATCH_SMOKE_SHOTS=1` is read as `SHOTS` for extra screenshots a step may take.

## Characterization tests

`tests/characterization.py` snapshots every read-only endpoint against a real library and diffs the result against a
recorded golden file. It is for refactoring: it shows any change in API output. Keep your own library in the
git-ignored `.seed/` folder (`penguwatch.db` and `golden.json`) and run `scripts/test.sh --seed`; without `.seed/` the
test is skipped. Record a new golden after an intended change with
`scripts/test.sh --seed --command python -m tests.characterization record /seed/penguwatch.db /seed/golden.json`.
`.seed/` holds your viewing history: never commit it.

## Project layout

```
penguwatch/
├── config.example.yaml         # optional config.yaml: listen address and database; read once at first start for the rest
├── Dockerfile / Dockerfile.test / .dockerignore   # the image is built from the wheel (stage "wheel")
├── docker-compose.yml          # builds from source; + docker-compose.override.example.yml for machine-specific additions
├── compose.release.yaml        # runs the published image (saved as compose.yaml by users)
├── CHANGELOG.md, .github/workflows/   # ci.yml and release.yml (docs/releasing.md)
├── .env.example                # variables read by docker-compose.yml from a git-ignored .env (pins and secrets)
├── scripts/deploy.sh           # maintainer's deploy: dependency gate + backup + build + restart (rootless Podman)
├── scripts/test.sh             # the test suite in a container
├── scripts/test-ui.sh          # the browser tests (tests/ui) in a locked-down container; Dockerfile.ui, scripts/lib-test-image.sh
├── requirements.in / -dev.in / -build.in   # direct dependencies as ranges (edited by hand)
├── requirements*.txt           # fully pinned and hashed closure (generated)
├── src/homeguard/              # request-guard library, vendored verbatim (see below)
├── src/penguwatch/
│   ├── main.py                 # app factory, lifespan frame and the ordered COMPONENTS list (see architecture.md)
│   ├── components.py           # the component contract: Component, Context, run_components
│   ├── notify_component.py, telemetry_component.py   # components that sit beside the module they run
│   ├── cli.py, paths.py        # the `penguwatch` command; per-user config and data folders
│   ├── config.py               # YAML config + EnvOverrides, the typed, validated environment variables
│   ├── config.example.yaml     # copy of the root config.example.yaml (a test keeps them identical)
│   ├── models.py               # Pydantic models
│   ├── security.py             # this app's profile for homeguard: CSP, public paths, calendar key
│   ├── api/                    # REST API, one module per domain, listed in __init__.ROUTERS and mounted by
│   │                           #   router.py; schemas.py holds validated request bodies, deps.py the AppContainer
│   │                           #   and Depends providers, component.py installs the container
│   ├── db/                     # repository (a facade of mixins: shows, episodes, services, titles, views,
│   │                           #   outbox, reports, state), schema.py, numbered migrations/, storage.py (component)
│   ├── drivers/                # base.py (capabilities, neutral types), __init__.py (the registry), one package
│   │                           #   per driver: simkl/ (client, auth, mapper, payloads, service: the capabilities)
│   ├── sync/                   # the generic sync engine: engine (orchestration), pull, push (outbox), actions,
│   │                           #   enrich (catalogue chain), titles (neutral -> stored), ops, backoff, active
│   │                           #   (running drivers), component.py (drivers + engine), worker.py (background sync)
│   ├── optimizer/              # engine (scores, verdicts), bundles, plannable, planner (rotation plan): pure;
│   │                           #   component.py builds the engine
│   ├── library.py, interest.py, insights.py, textsearch.py   # pure rules, tested alone
│   ├── ratings.py, events.py   # rating sources (models, scales, title matching) and the event names and envelope
│   ├── webhooks.py, webhooks_component.py   # signed, bounded event delivery, and the component that runs it
│   ├── observability.py        # in-process metrics, request ids, JSON logs, Prometheus text
│   ├── telemetry.py, grafana.py   # OpenTelemetry export and the Grafana links
│   ├── notify.py, activity.py, clock.py, pwa.py
│   ├── ui.py                   # finds the dashboard's tab and dialog folders (see docs/ui.md)
│   └── web/                    # the page shell, design tokens and components, js/ (the shell's ES modules),
│                               #   panels/<tab>/ and dialogs/<id>/ (markup, module, styles per folder)
├── docs/examples/              # the runnable scripts docs/extending.md shows (tests/test_examples.py runs them)
└── tests/                      # pytest; conftest.py provides a fully wired `container` fixture
```

The backend is an ordered list of components, not one long start-up function: [Architecture](architecture.md).
Request handlers get their dependencies from the `AppContainer` through FastAPI's `Depends`. Module-level state is
limited to the in-process metrics, the activity tracker, the rate-limit counters and the telemetry providers.

## homeguard

`src/homeguard/` is a small request-guard library (host and origin checks, access token and sign-in, body caps, rate
limits, security headers) and a SQLite reader/writer helper, shared with other apps and copied in verbatim.
`tests/test_vendored_homeguard.py` checks the copy against its manifest of hashes, so do not edit it here: change it
upstream and copy it again.

## Database migrations

Add a module `src/penguwatch/db/migrations/NNNN_<name>.py` with `async def migrate(conn)` and register it in
`migrations/__init__.py`. Each migration runs once, in its own transaction, and is recorded in `schema_migrations`.
Do not use `executescript` in a migration (it commits implicitly).

## Dependencies

See [Supply chain](supply-chain.md). To change a dependency, edit the `.in` file, re-lock, review the diff and commit
the generated `requirements*.txt`; never edit those by hand.
