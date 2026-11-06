# Architecture: components

The backend is an ordered list of components, `COMPONENTS` in `src/penguwatch/main.py`. `main.py` itself holds only
the app factory (middleware, static files and PWA routes, the dashboard page, routers), the lifespan frame (telemetry,
config, logging level, clock, umask) and that list. Each part builds itself in its own module. Adding a part is a
module with a `register` hook and one line in the list; `tests/test_components.py` checks that nothing else names it.

## The contract

A component is a module with

```python
async def register(ctx: Context) -> Component | None
```

and, optionally, `OPTIONAL = True`, for a part the app can run without (see below).

`register` builds the part from `ctx` and returns what the app runs for it, or `None` when there is nothing to run.
`ctx` (`components.Context`) carries:

- `app`, `config` (the loaded `AppConfig`), `env` (`EnvOverrides`, the environment variables read once), `security`
  (the request guards) and `otel` (the telemetry handle);
- what an earlier component built for later ones: `repository`, `drivers` (the running drivers), `sync` (the sync
  engine), `optimizer`, `container`, `notifier`. A field is `None` until its component has registered, which is why the order is explicit;
- `spawn(coro)`, which starts a background task that runs until shutdown. A task that dies of an exception is logged
  when it dies.

The returned `Component` has a `name` and two optional parts:

- `start()`: after every component has registered, in list order. It starts background work with `ctx.spawn`.
- `stop()`: at shutdown, after every spawned task has ended, in reverse list order. It closes what the part owns.

## The order, and why

1. **storage** (`db/storage.py`). Opens (migrates) the database and gives the request guards the session store next
   to it. Every later component reads the repository, and its `stop` runs last, so it closes after everything that
   writes.
2. **drivers** (`sync/component.py`). Builds each driver config.yaml's `drivers:` names, once, with its stored login
   (see [The driver layer](#the-driver-layer)), and the sync engine on the library driver. Needs the repository. Its
   `stop` closes the drivers' HTTP connections.
3. **optimizer** (`optimizer/component.py`). Needs the repository.
4. **container** (`api/component.py`). Builds the `AppContainer` the request handlers read (`Depends(get_*)`) and
   installs it on the app, once the parts it holds exist.
5. **sync worker** (`sync/worker.py`). Hooks the activity tracker and spawns the background loop that pushes queued
   changes and pulls what changed. Its `stop` makes one last attempt (10 s at most) to deliver the outbox, which has
   to happen while the drivers are still open: it stops before the drivers component's `stop` because it is later in
   the list.
6. **notifications** (`notify_component.py`). Optional. Puts the notifier on the container and, when a notification
   URL is set, spawns the daily digest worker.
7. **webhooks** (`webhooks_component.py`). Optional. Reads `webhooks:` from the config, resolves each secret (an
   environment variable or a file; one that cannot be read keeps its webhook off, never unsigned), puts the
   dispatcher on the container and connects it to the repository's data-change events. `start` spawns one delivery
   task per webhook (own bounded queue, so a dead receiver delays only itself) and the `episode.airs_today` check.
   It comes after the container (it sits on it) and the database (it listens to its commits). See
   [Extending](extending.md).
8. **telemetry sampler** (`telemetry_component.py`). Optional, and only when OpenTelemetry exports metrics. Spawns the
   loop that measures event-loop lag and refreshes the state gauges from the finished container, so it is last.

The order is explicit, never discovered: start-up has real dependencies, and a reader should see everything that runs,
in the order it runs.

## Start-up failures

- A **required** component's failure in `register` or `start` stops the start with its own message, after the
  components that had already registered have been stopped (the database is closed again, not left open).
- An **optional** component (`OPTIONAL = True`: notifications, webhooks, the sampler) is contained by the frame. The error is
  logged ("notify_component could not register: ...; running without it") and the app starts without that part.
  One whose `start` failed is still stopped.
- A malformed environment variable (`PENGUWATCH_GRAFANA_URL`, `PENGUWATCH_NOTIFY_HOUR`) stops the start: the values
  are parsed and validated once, in `EnvOverrides`, before any component runs.

## Shutdown

`podman stop` and `docker stop` send SIGTERM and kill the container after the stop timeout (20 s in the compose files). Uvicorn then runs the lifespan's shutdown:

1. Every task the components spawned is cancelled, and the frame waits for all of them.
2. Each component's `stop` runs in reverse list order. A stop that fails is logged and the others still run:
   the sync worker flushes the outbox, the drivers close, the database closes.
3. The frame logs "PenguWatch shut down.".
4. The telemetry flush comes last, so the shutdown's own log lines and spans go out too.

`tests/test_components.py::test_shutdown_order` checks this.

## The driver layer

Everything PenguWatch learns from or sends to a service goes through a **driver**: an in-tree package in
`src/penguwatch/drivers/` (an explicit registry, `DRIVERS` in `drivers/__init__.py`, no plugin loading, so the
dependency gate sees everything that runs). A driver declares the **capabilities** it offers, each one small interface
in `drivers/base.py`:

| Capability | Interface | What it does |
|---|---|---|
| `catalog` | `CatalogProvider` | search, a title's details, its episode list, its sequels, resolving another provider's id |
| `library_sync` | `LibrarySync` | an account's library: a connect flow the page renders generically, a pull that yields neutral changes, a push of neutral operations, the login status |
| `schedule` | `ScheduleProvider` | upcoming and airing episodes |
| `discovery` | `DiscoveryProvider` | trending titles, recommendations for a title |
| `ratings` | `rating_sources` | built-in rating sources; their values travel on `Title.ratings` |

config.yaml's `drivers:` names the driver for each role ([Configuration](configuration.md#drivers)): one library
driver, and a list for the catalogue, schedule and discovery (the first that knows the title wins). The drivers
component builds each named driver once; `ActiveDrivers` (`sync/active.py`) holds them by name and by role.

Outside the drivers everything speaks the **neutral model** of `drivers/base.py`: a title is identified by its ids,
one per provider (`title_ids`; `{"simkl": 1, "imdb": "tt…", "tmdb": 2}`), an episode by season and number, a local
change is a `SyncOp` (watched, unwatched, status, rate, unrate, remove). Each driver keeps its state in app_state under
`driver.<name>.*`. Nothing outside a driver's package names its service or imports it (tests/test_driver_layer.py).

The **sync engine** (`sync/`) is generic and runs on the library driver:

- `engine.py`: a sync run (push, pull, enrichment, schedule), the reconnect gate, the errors a driver reports.
- `push.py`: the outbox. Every local change is a neutral operation for one driver, waiting for approval in review
  mode, delivered in order per title, backing off after a failure; ops the driver says may share a request
  (`LibrarySync.batch_key`) go out together.
- `pull.py`: the driver plans a pull from its stored cursor and yields the changes a part at a time; the engine
  applies or stages them (review mode), never overwrites a title with unsent changes, removes what left the library,
  and stores the new cursor only once everything was applied.
- `actions.py`: the local edits (episodes, seasons, status, rating) that queue operations.
- `enrich.py`: details, episode lists and sequels from the catalogue chain, with a back-off per title; the schedule.
- `titles.py`: neutral titles turned into what the library stores.
- `worker.py`: the background loop (the driver's pull intervals; its `maintain()` renews a login ahead of expiry).

**The local driver.** `drivers/local/` is a library driver with `remote = False`: no account sits behind the library, so
the engine never pushes or pulls (it only keeps edits for the last account driver as unapproved outbox rows), reports review mode as off, and the worker only does the
catalogue upkeep (details, episode lists, the schedule). Titles added while it is active get a `local` id
(`LOCAL_ID`), which a later pull from an account's listing reads as "never on the account". Changes queued for an
account driver stay in the outbox when the library driver changes, and are delivered when it is the library driver
again ([Drivers](drivers.md#switching-the-library-driver)).

**Rating storage.** A rating source is stored one of two ways. A source with a `column` (SIMKL's and IMDb's, in
`shows`) keeps its value in that column. A source without one (TVmaze's) and every custom source keep theirs in
`title_ratings`: a row per title and source, with the value, its votes and a link to the title's page. A driver's
column-less source is built in: it is listed read-only (`/api/rating-sources`), only its driver fills it
(`store_ratings` in `sync/enrich.py`, when a title's details are fetched), and it exists while a configured driver
declares it (`stored_builtin_specs`). Values travel from a driver on `Title.ratings`
([Drivers](drivers.md#built-in-rating-sources)).

The routes are generic too: `/api/drivers/{driver}/status|connect|credentials|disconnect|sync|diagnose` for the
library driver, `/api/catalog/search` for the catalogue. [Drivers](drivers.md) describes the drivers that ship and
how to write one.
