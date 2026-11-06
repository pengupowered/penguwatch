# Changelog

All notable changes to PenguWatch are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.15.2] - 2026-11-06

### Fixed
- The project page on PyPI shows the README's screenshots, logo and links: they now use full GitHub addresses, since
  PyPI cannot follow the relative paths that only work on GitHub.

## [0.15.1] - 2026-11-06

### Fixed
- Tidy up: the "untouched for" period is read as a number before it is shown (it was put into the page as it came).
- Release notes on GitHub link to the documents of that version instead of relative paths that did not work there.

### Changed
- The README shows the PyPI version, the release workflow status and the licence.

## [0.15.0] - 2026-11-06

The first public release.

### Changed
- **Drivers.** Everything PenguWatch learns from or sends to a service goes through a driver that declares its
  capabilities (catalogue, library sync, schedule, discovery, ratings); SIMKL is the default driver for every role, and
  config.yaml's new `drivers:` section picks the driver per role ([docs/drivers.md](docs/drivers.md)). The sync engine
  (outbox, review mode, back-off, pulls) no longer depends on SIMKL. Your library, watch history, ratings, pending
  changes and SIMKL login carry over: the database migrates on the first start.
- **API (breaking):** titles, episodes, search and discover results carry `ids` (`{"simkl": 1, "imdb": "tt…"}`)
  instead of `simkl_id`, `imdb_id`, `tmdb_id` and `tvdb_id`; adding a title takes `ids`. The SIMKL routes moved to
  `/api/drivers/simkl/...` (status, connect, credentials, disconnect, sync, diagnose) and `/api/catalog/search`;
  `DELETE /api/shows/{id}` takes `remove_from_library`. Webhook payloads are version 2 (a title's `ids`). The
  Prometheus gauges `penguwatch_simkl_*` are `penguwatch_library_*{driver}`, the OpenTelemetry metrics
  `penguwatch.simkl.*` are `penguwatch.upstream.*` (by service), `penguwatch.sync.changes` and `penguwatch.library.*`.

### Fixed
- Keeping edits for the account: a newer change replaces the older unapproved ones it contradicts; an add made before
  the first login only adds when the account lacks the title (the account's state wins otherwise); unapproved rows of
  an inactive driver cannot be declined; a batch whose push raised an unexpected error is not resent at once.
  The adds held before the first login are resolved only after the whole full pull succeeded; edits made meanwhile wait
  for your decision instead of being dropped; rows that cannot be declined can be dropped without restoring; a trimmed
  watched row's description follows its episodes; a newer status removes an older unapproved removal.

### Added
- **A `local` library driver:** `drivers: {library: local}` keeps your library, watch history and ratings only in
  PenguWatch, with no account (titles still come from the catalogue). Nothing is queued, sent or pulled, and the
  header reads "Local only". Switching between `simkl` and `local` deletes nothing: changes queued for SIMKL wait and
  are delivered when it is the library driver again, and titles added while local are never removed by a later pull.
  Edits you make while local to titles SIMKL knows are kept as unapproved changes: after switching back they wait in
  Review pending changes, a pull leaves those titles alone, and you approve (send) or decline (take SIMKL's version) them.
- **`drivers: {schedule: [], discovery: []}`** turn the Air Schedule and calendar feed, or Discover, off (the tab
  shows a short note). `catalog` still needs a driver.
- The page no longer names a service: the header pill, Services & Sync, the connect flow, review, the add dialog,
  details links and the rating sources all come from `GET /api/drivers`, which now also reports each driver's label,
  connect kind, extras, title link template and rating sources, whether the library has an account (`remote`) and how
  many changes wait for an inactive driver. In SIMKL mode the page looks and works as before.
- **A TVmaze driver (no account needed).** `drivers: {catalog: [tvmaze, simkl], schedule: [tvmaze]}` finds TV shows
  at [TVmaze](https://www.tvmaze.com/api), fills their episode lists, upcoming episodes and TVmaze rating, and
  PenguWatch credits TVmaze as its licence (CC BY-SA 4.0) asks ([docs/drivers.md](docs/drivers.md#tvmaze)). A rating
  source of a driver may now live in `title_ratings` instead of a column of `shows`; such built-in sources are listed
  read-only in `/api/rating-sources`, and `GET /api/drivers` lists each driver's `attribution`.
- **Driver credits in the page.** Every running driver whose data licence asks for a credit (today TVmaze: "TV data from
  TVmaze", linked) is credited in a line at the foot of every tab, on Services & Sync and in the details of a title that
  has its id. The text and link come from `GET /api/drivers`; with the default SIMKL setup nothing is shown.
- **Docs: writing a driver.** [Drivers](docs/drivers.md#writing-a-driver) is now a complete guide (terms to check, package
  layout, class attributes, capabilities and errors, ids, the rate limiter, rating sources, the guard tests, recorded-answer
  tests, a worked catalogue-only example). The SIMKL sync page is now [Sync](docs/sync.md): the generic engine, then
  SIMKL. The README says how to run without a SIMKL account and what that gives up, and `config.example.yaml` has a
  commented example.
- **Native installs on Windows, macOS and Linux, no container needed.** `install.sh` (Linux, macOS) and `install.ps1`
  (Windows) are release assets: they check the wheel and the hash-locked dependency list against `SHA256SUMS`, download a
  pinned [uv](https://docs.astral.sh/uv/) (checked by sha256), install a private Python 3.12 and the locked
  dependencies (`--require-hashes --only-binary :all:`), and start PenguWatch in the background on
  `http://127.0.0.1:8080`. Options: `--version`, `--host`, `--port`, `--user`, `--no-service`, `--no-browser`, `--yes`,
  `--from DIR`, `--uninstall` (your data stays). Running the script again upgrades, after a backup and with the listen
  address kept. The container stays a deployment option. See [docs/install.md](docs/install.md).
- **`penguwatch service install|uninstall|status`** (with `--user`, `--host`, `--port`, `--dry-run`): a hardened systemd
  unit (throw-away user, no capabilities, read-only system; `systemd-analyze security` about 1.2) or a user unit, a
  macOS LaunchDaemon with its own account or a LaunchAgent, or a Windows logon task that runs the new windowless
  `penguwatchw`.
- **`penguwatch backup`** copies the database (a consistent snapshot, safe while it runs) and the sessions file into a
  timestamped folder, and **`penguwatch doctor`** checks the port, config file, data folder, time zone and SIMKL.
- **`penguwatch.env`**, next to `config.yaml`: where a native install keeps the access token, the notification URL and
  other secrets. The installer creates it empty and owner-only; the program loads it however it was started, and no
  secret is written into a service definition or printed.
- CI installs the release files with the scripts on Linux, macOS and Windows, checks that the service answers, that the
  settings file reaches it, an upgrade and the uninstall.

### Changed
- `tzdata` is a dependency on Windows, which has no time zone database of its own (the household time zone needs one).

## [0.14.0] - 2026-11-04 (not published)
The first public release.

### Added
- **Install it two ways.** `uv tool install penguwatch` (or `pipx install penguwatch`) gives you a `penguwatch`
  command, and the container image `ghcr.io/pengupowered/penguwatch` (Intel/AMD and ARM, including a Raspberry Pi)
  runs with Docker or Podman. The image is built from the very same package, and `compose.release.yaml` runs it.
  See [docs/install.md](docs/install.md).
- **`penguwatch` command:** `penguwatch` (or `penguwatch serve`) starts the server, with `--host`, `--port`,
  `--config` and `--data-dir`; `penguwatch init-config` writes an example `config.yaml` for you to edit;
  `penguwatch paths` shows where your config and data are; `penguwatch --version`.
- **Per-user folders.** Installed with uv or pipx, the config and the database live in your own folders (on Linux
  `~/.config/penguwatch` and `~/.local/share/penguwatch`, with the usual places on macOS and Windows). It listens on
  this computer only (`127.0.0.1`) unless you say otherwise.
- **Tracking:** every show you are watching with its exact next episode, episode-level tracking for shows, anime and
  movies, and Tonight's Pick.
- **Two-way SIMKL sync** with an outbox that survives restarts and a review mode where nothing is sent or applied
  until you approve it.
- **Subscription optimizer and rotation planner:** which services to keep, pause or cancel month by month, with
  bundles, renewal dates, price history and cost per hour watched.
- **Discover, household and calendar:** trending and "because you liked" picks, two people with "who wants to watch",
  an iCal feed with renewal and "cancel by" reminders, and a daily ntfy or webhook digest.
- **Your own rating sources.** Bring ratings from anywhere (Letterboxd, a spreadsheet, a friend): define a source in
  `config.yaml` or the API, import up to 1000 values at a time matched by id or by title and year, then sort
  (`sort_by=rating:<source>`) and filter (`min_rating=<source>:<value>`) the library by it. The CSV export gets a column
  per source and the backup carries the values. SIMKL and IMDb appear in the same list as built-in sources.
- **Event webhooks.** `webhooks:` in `config.yaml` sends signed JSON (HMAC-SHA256) to your URLs for `title.added`,
  `title.status_changed`, `title.rating_changed`, `episode.watched`, `episode.airs_today` and `sync.failed`, with
  bounded queues, retries and no redirects; their state is on the System tab and in `/metrics`.
- **A documented API.** Every route has an OpenAPI tag and summary, [docs/api.md](docs/api.md) lists the stable
  endpoints, and [docs/extending.md](docs/extending.md) has complete examples for pushing ratings and receiving webhooks.
- **Built for a home network:** an optional access token, protection against DNS rebinding and cross-site requests,
  a non-root read-only container, Prometheus metrics and optional OpenTelemetry export.

### Changed
- The example `config.yaml` no longer sets `server.host` or `database.db_path`; leave them out to get the defaults
  above. The container and `docker compose` behave as before, and your existing `config.yaml` keeps working.

[Unreleased]: https://github.com/pengupowered/penguwatch/compare/v0.15.2...HEAD
[0.15.2]: https://github.com/pengupowered/penguwatch/compare/v0.15.1...v0.15.2
[0.15.1]: https://github.com/pengupowered/penguwatch/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/pengupowered/penguwatch/releases/tag/v0.15.0
