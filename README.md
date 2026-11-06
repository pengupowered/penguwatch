<p align="center">
  <img src="docs/images/logo.png" width="112" height="112" alt="">
</p>

<h1 align="center">PenguWatch</h1>

<p align="center">
  <a href="https://pypi.org/project/penguwatch/"><img src="https://img.shields.io/pypi/v/penguwatch" alt="PyPI version"></a>
  <a href="https://github.com/pengupowered/penguwatch/actions/workflows/release.yml"><img src="https://github.com/pengupowered/penguwatch/actions/workflows/release.yml/badge.svg" alt="Release workflow status"></a>
  <a href="https://github.com/pengupowered/penguwatch/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

<p align="center">
  <b>Know what to watch next, and pay only for the streaming services you actually use.</b><br>
  A self-hosted TV, anime and movie tracker with a streaming-subscription rotation planner, and two-way sync with your tracking account (SIMKL by default) or no account at all.
</p>

<p align="center">
  <img src="docs/images/up-next-dark.png" width="860" alt="Up Next: the next episode of every show in progress, with posters, services and progress">
</p>

<p align="center">
  <img src="docs/images/library-light.png" width="420" alt="The library in the light theme, with a custom rating badge">
  <img src="docs/images/optimizer-dark.png" width="420" alt="The subscription optimizer advising which service to cancel">
  <img src="docs/images/phone-up-next.png" width="150" alt="Up Next on a phone">
</p>

<p align="center"><sub>Screenshots of the made-up demo library (<code>scripts/demo_library.py</code>). More in <a href="docs/screenshots.md">the gallery</a>.</sub></p>

PenguWatch is for households that follow shows, anime and movies across several streaming services and want one place
that says what to watch next and which subscriptions are worth keeping. You run it yourself, on a home server, a NAS or
just your own computer: one small program with one SQLite database, used from any browser on your network (and as an
installable phone app). It keeps your library in step with a tracking account, the **library service**
([SIMKL](https://simkl.com) out of the box), or keeps it only in PenguWatch with no account at all. Each service is a
[driver](docs/drivers.md); this README says "the library service" for whichever one holds your library.

## Why

Most households pay for more streaming services than they watch in a given month, and lose track of where each show
is. PenguWatch keeps your library in sync with your library service, shows the exact next episode of everything
you're watching, and plans which services to keep, pause or cancel month by month, based on what you actually have
left to watch on each.

It runs as one small container on a home server or NAS (or as a Python program installed with uv or pipx), keeps everything in a local SQLite database, and works from
any browser on your network, including as an installable phone app.

## Features

- **Up Next:** every show you're watching with its exact next episode (`S02E05 – Title`), progress and episodes left.
  One tap marks it watched and sends that episode to the library service.
- **Episode-level tracking** for shows, anime and movies: whole seasons, single episodes or "first N" at once; only
  the episodes that changed are sent to the library service.
- **Two-way sync** with the library service, fetching only what changed since the last sync, with an outbox that survives restarts and a
  **review mode** (on by default) where nothing is sent or applied until you approve it.
- **Drivers:** each service is a driver ([docs/drivers.md](docs/drivers.md)): SIMKL (the default), a `local` driver that
  keeps your library only in PenguWatch, and a TVmaze driver that finds TV shows without any account. The guide shows how
  to write another one.
- **Subscription optimizer and rotation planner:** ranks services by backlog hours, ready-to-binge seasons and
  ratings; plans the next 3, 6 or 12 months within a limit of active services; understands bundles (for example
  Disney+ & Hulu); tracks renewal dates, price history and cost per hour watched.
- **Discover:** trending titles and "because you liked X & Y" picks from the discovery service, marked with what you already have and
  which of your services carries them.
- **English titles and forgiving search:** anime stored under romaji titles show their English name first; search
  ignores accents, case, punctuation and long vowels ("shogun" finds *Shōgun*).
- **Household:** optionally two people, each title tagged with who wants to watch it; "both want it" titles count
  more in the plan. Names come from your configuration.
- **Tonight's Pick, air schedule and an iCal feed** for your calendar app, including renewal and "cancel by"
  reminders.
- **Alerts and insights:** a daily ntfy or webhook digest, monthly hours, hours by service, top genres.
- **Your own ratings:** add rating sources (a site you follow, a friend, a spreadsheet), import them from CSV or the
  API, then show them as badges, sort and filter by them. The ratings the catalogue drivers provide are built in (SIMKL's and IMDb's, or MyAnimeList's for anime, from SIMKL;
  TVmaze's from TVmaze).
- **Extensible from outside:** a documented REST API ([docs/api.md](docs/api.md)) and signed event webhooks
  (title added, status or rating changed, episode watched or airing today, sync failed); examples in
  [docs/extending.md](docs/extending.md).
- **Light and dark themes**, following your system until you pick one.
- **Built for a home network:** optional access token, DNS-rebinding and cross-site protection, a strict CSP, a
  non-root read-only container, Prometheus metrics and optional OpenTelemetry export.

## Quick start

Out of the box PenguWatch syncs with a free SIMKL account, but you can [run without one](#without-an-account). PenguWatch runs on your own computer, starting by itself in the background, or in a
container: pick one.

**Windows** (PowerShell, no administrator rights):

```powershell
irm https://github.com/pengupowered/penguwatch/releases/latest/download/install.ps1 | iex
```

**macOS and Linux** (asks for sudo once; end the line with `sh -s -- --user` instead to avoid it):

```sh
curl -fsSL https://github.com/pengupowered/penguwatch/releases/latest/download/install.sh | sh
```

The scripts check every download (pinned uv, hash-locked dependencies), install a private Python 3.12, and start
PenguWatch on `http://127.0.0.1:8080`, this computer only. Per-system details, upgrading and uninstalling:
[Install](docs/install.md).

**With uv or pipx** (Python 3.11 or newer):

```bash
uv tool install penguwatch        # or: pipx install penguwatch
penguwatch init-config            # writes an example config.yaml to edit (your services, prices, household names)
penguwatch                        # http://127.0.0.1:8080, this computer only
penguwatch service install --user # optional: start it by itself (systemd, launchd or a logon task)
```

**With a container** (Docker, or Podman with podman-compose; Intel/AMD and ARM):

```bash
mkdir penguwatch && cd penguwatch
curl -fsSL https://raw.githubusercontent.com/pengupowered/penguwatch/main/compose.release.yaml -o compose.yaml
curl -fsSL https://raw.githubusercontent.com/pengupowered/penguwatch/main/config.example.yaml -o config.yaml
docker compose up -d              # or: podman compose up -d
```

Or build it yourself from a clone: `cp config.example.yaml config.yaml`, then `docker compose up -d --build`. Open
`http://<server-ip>:8080`. [Install](docs/install.md) has the details: where config and data live, upgrading, backups.
Then connect the default library service, SIMKL (the first run, with local and TVmaze instead, is in [Getting started](docs/getting-started.md)):

1. Create a SIMKL app once at [simkl.com/settings/developer](https://simkl.com/settings/developer/app/new) and copy
   its client ID.
2. In PenguWatch, open **Services & Sync**, enter the client ID under *Enter Client ID / Token Manually*, then choose
   **Connect SIMKL with Device PIN** and approve the code at simkl.com/pin. The first sync imports your library.
3. Mark the services you subscribe to this month and check their prices.

> [!IMPORTANT]
> Without an access token, anyone who can reach port 8080 can read and change your library, **connect PenguWatch to
> their own library-service account** (your later changes are then sent to them), approve queued changes, download a backup and
> trigger the webhook and notification tests. Both compose files publish `0.0.0.0:8080`, so a container is reachable on
> your whole network from its first start: set `PENGUWATCH_ACCESS_TOKEN` first (in `.env` for the container, in
> `penguwatch.env` next to `config.yaml` for a native install; see [Install](docs/install.md#settings-and-secrets) and
> [Security](docs/security.md)). Over plain HTTP the token can be read by others on the network: use HTTPS through a
> reverse proxy if that matters. The `penguwatch` command listens on this computer only unless you pass `--host 0.0.0.0`.

Your data survives upgrades: it lives in the `penguwatch_data` volume (container) or your data folder
(`penguwatch paths`). To update, run the install script again (or `uv tool upgrade penguwatch`), or raise the image tag
and `docker compose up -d`;
database migrations run on start.

## Without an account

SIMKL is the default library service, but PenguWatch works with no account. In `config.yaml`:

```yaml
drivers:
  library: local                    # your library, watch history and ratings live only in PenguWatch
  catalog: [tvmaze]                 # find and add TV shows at TVmaze (no account or key)
  schedule: [tvmaze]                # upcoming episodes, from TVmaze
  discovery: []                     # no Discover tab
```

You can still add shows, track episodes, rate them, get the air schedule and calendar feed, and use the subscription
optimizer and planner. What you lose compared with a library service:

- **Sync:** nothing is sent to or read from an account, so an existing account library is not imported and no other device
  or app sees your changes. Back up the database ([Operations](docs/operations.md)).
- **Discovery:** TVmaze has no trending lists or recommendations, so Discover is off.
- **Movies:** TVmaze has no movies, so a movie cannot be searched for and added (the catalogue list can name more than
  one driver: `[tvmaze, simkl]` adds SIMKL's catalogue, which then needs a SIMKL app id).
- **Anime:** TVmaze lists an anime as a TV show with the Anime genre, which PenguWatch types as anime; it has no
  sequels and no anime-specific data beyond that genre.
- **Ratings:** TVmaze's average only, with none of the scores a library service's catalogue brings (SIMKL's and IMDb's, with the default).

TVmaze's data is CC BY-SA 4.0, so PenguWatch credits TVmaze (in the page footer, on Services & Sync and in a title's
details) as its licence asks. [Drivers](docs/drivers.md) has the details, and `library: simkl` with
`catalog: [tvmaze, simkl]` mixes both.

## Configuration in brief

`config.yaml` holds the server settings, the sync schedule, the optimizer's weights, your household and the streaming
services with their network aliases (used to work out which service carries a title). For example:

```yaml
household:
  members: ["Alex", "Sam"]     # one name hides the "who wants to watch" field

optimizer:
  max_active_services: 3       # how many paid services the planner may keep at once
```

`.env` holds secrets and deployment settings (`PENGUWATCH_*`). Everything is described in
[Configuration](docs/configuration.md).

## Documentation

New here? [Getting started](docs/getting-started.md) walks through the first run. [docs/README.md](docs/README.md) is the
index of everything.

| Topic | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | The first run end to end: install, services, the library service or local, the first sync |
| [FAQ and troubleshooting](docs/faq.md) | Port in use, expired login, nothing syncs, wrong service, time zone, reverse proxy, backup, upgrade, rollback |
| [Install](docs/install.md) | Windows, macOS, Linux, Raspberry Pi, uv or pipx, container, the background service, upgrading, backups |
| [Configuration](docs/configuration.md) | `config.yaml`, environment variables, household, services and bundles, time zone |
| [Using PenguWatch](docs/usage.md) | The tabs, the optimizer and planner, the calendar feed, shortcuts, the phone app |
| [Sync](docs/sync.md) | How the sync engine works (outbox, review mode, removals), then the SIMKL driver's rules: API usage, login renewal |
| [Drivers](docs/drivers.md) | The drivers that ship (SIMKL, local, TVmaze), switching, and a guide to writing one |
| [API](docs/api.md) | Authentication, the stable endpoints and their shapes, errors, limits |
| [Extending](docs/extending.md) | Your own rating sources, event webhooks, drivers, adding a component |
| [Security](docs/security.md) | Access token, request guards, the container, what is exposed |
| [Operations](docs/operations.md) | Monitoring, Prometheus, OpenTelemetry, notifications, backups and exports |
| [Architecture](docs/architecture.md) | The ordered list of backend components, start-up failures, shutdown |
| [Development](docs/development.md) | Project layout, running locally, tests |
| [The web UI](docs/ui.md) | How the page is built, theming, adding a tab, the rules the tests enforce |
| [Releasing](docs/releasing.md) | CI, the release workflow, PyPI and the container registry |
| [Supply chain](docs/supply-chain.md) | Dependency policy, hash-locked requirements, vendored code |

**Security in one line:** one shared access token protects the app, which is meant for a home network and not the open
internet; set it, and read [docs/security.md](docs/security.md). Found a vulnerability? Report it privately
([SECURITY.md](SECURITY.md)).

## Contributing

Bug reports, ideas and code are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md).

## Development

```bash
pip install --require-hashes --no-deps -r requirements-dev.txt
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m penguwatch.main      # http://localhost:8080, using ./config.yaml or the example
scripts/test.sh                               # the test suite in a container, nothing installed locally
```

Dependencies are hash-locked (`requirements*.txt`, generated from the `.in` files) and installed with
`--require-hashes`. `scripts/deploy.sh` is the maintainer's deploy for rootless Podman: it adds a dependency audit (the
separate `depguard` tool), a rollback image and a database backup before rebuilding. `docker compose` is all you need
to run PenguWatch.

## License

[MIT](LICENSE). PenguWatch is not affiliated with SIMKL or TVmaze; its default driver uses SIMKL's public API with your own client ID.

**TVmaze credit:** when the TVmaze driver is used, TV data comes from [TVmaze](https://www.tvmaze.com) under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), and PenguWatch says so in the page footer, on Services &
Sync and in a title's details ("TV data from TVmaze", linked to tvmaze.com).
