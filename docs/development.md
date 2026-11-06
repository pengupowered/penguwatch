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
├── config.example.yaml         # copy to config.yaml: server, sync, optimizer, household, services
├── Dockerfile / Dockerfile.test / .dockerignore   # the image is built from the wheel (stage "wheel")
├── docker-compose.yml          # builds from source; + docker-compose.override.example.yml for machine-specific additions
├── compose.release.yaml        # runs the published image (saved as compose.yaml by users)
├── CHANGELOG.md, .github/workflows/   # ci.yml and release.yml (docs/releasing.md)
├── .env.example                # settings read by docker-compose.yml from a git-ignored .env
├── scripts/deploy.sh           # maintainer's deploy: dependency gate + backup + build + restart (rootless Podman)
├── scripts/test.sh             # the test suite in a container
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
