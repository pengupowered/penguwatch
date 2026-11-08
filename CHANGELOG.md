# Changelog

All notable changes to PenguWatch are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.16.0] - 2026-11-07

### Added
- **Settings tab.** Everything that was in `config.yaml` is now changed in the app: household names, the sync schedule,
  the optimizer's weights, the daily digest (address, kind, hour, renewal warning), event webhooks, the theme and time zone,
  MCP, the Grafana address, which driver fills each data role, and a few advanced values. Changes apply at once (data
  sources, the SIMKL app name and the TVmaze request limits say "applies after a restart"), are checked against their
  ranges, are kept as versions (the last 50, restorable), and two open tabs cannot overwrite each other. A setting fixed by
  an environment variable shows "From environment" and is read-only. Export writes the settings as YAML (no address or
  secret) and Import reads such a file into the form for review. `GET /api/settings/schema`, `/api/settings`,
  `/api/settings/values`, `/validate`, `/versions`, `/export`, `/import` and `PUT /api/settings` serve it
  ([API](docs/api.md#settings); not stable, and guarded by the access token like every `/api` route). Four values that
  were fixed in the code are settings now: the renewal warning days, the optimizer's viewing window, its "not used"
  threshold and History's bulk-mark threshold.
- **`secrets.json`.** The digest address and each webhook's address and signing secret are kept in a file next to the
  database (mode 0600, written atomically), write-only through the API and never exported, logged or put in a version.
  The tab cannot point a webhook at a variable or a file for its signing secret: a `secret_env` or `secret_file` imported
  from `config.yaml` is kept read-only (or cleared with "Stop using it"), a new or changed one is refused. A typed
  signing secret needs 16 characters. An address (a webhook's or the digest's) must be `http(s)` without
  `user:password@`, and may not be this machine or a link-local address (`localhost`, `127.0.0.1`, `::1`, `169.254.x.x`,
  and numeric forms such as `2130706433`); the same check runs again on the resolved address when a call is made, so a
  name that later points at this machine is refused too. A webhook that signs with an imported variable or file can't
  be given a new address on the tab ([Security](docs/security.md#settings-and-secrets)).
- `penguwatch backup` and `scripts/deploy.sh` copy `secrets.json` with the database. `penguwatch settings check-import
  CONFIG DB` tries the one-time import of `CONFIG` on a copy of the database `DB` (which is never written) and reports
  what would change, never a value; `--live` compares an install that was already imported.
- Streaming services that came from the built-in list and that you never edited are brought up to date with the built-in
  list at each start (name, aliases, titles, bundle members; never prices, renewal dates or whether a service is active).
  Services you edited or added are never touched.
- **Edit on the Services tab.** Each streaming service has an Edit link that changes its name, price, aliases, titles
  and (for a bundle) its members in place, instead of removing the service and adding it again.
- **Browser tests in the repository.** `scripts/test-ui.sh` runs the dashboard in headless Chromium (and a pass in
  Firefox) in a locked-down container with no network ([docs/development.md](docs/development.md)).
- **History tab.** What you watched over the last 7 days, 30 days or 12 months, compared with the period before:
  hours, episodes, movies, titles started and finished, days with viewing, a chart over time, top shows, hours and
  cost per hour by service, genres, and weekday and time of day (in your time zone). Episodes marked in bulk (three or
  more with one exact timestamp) are left out unless you include them. `GET /api/history`. The demo library now has a
  year of watching, with a few bulk marks.
- **Themes.** Seven palettes (the standard look, Nord, Dracula, Solarized, E-ink, High contrast and Blueprint) picked per
  browser in the header's theme menu, which replaces the light/dark button and keeps light, dark and "like the system" as a
  separate choice. `display.theme` in `config.yaml` is the theme a browser shows until it picks one (an unknown name falls
  back to the standard look, with one warning in the log). Themes are palettes only: a folder of `theme.json` under
  `static/themes/`, checked in the tests for text contrast, status colours and the card edge ([docs/themes.md](docs/themes.md)).
  `GET /api/themes` lists them. The sign-in page follows light/dark only.
- **MCP endpoint.** An optional, read-only `POST /mcp` lets an AI assistant such as Claude Code ask about the library:
  `up_next`, `tonights_pick`, `search_library`, `title`, `schedule`, `optimizer`, `sync_status` and `history` (what
  was watched over 7, 30 or 365 days, the same data as the History tab). Off by default
  (`mcp.enabled` in config.yaml, or `PENGUWATCH_MCP_ENABLED`); guarded like the API (access token, Host and Origin
  checks, body cap); no secret is ever in an answer; calls are counted as `penguwatch_mcp_calls_total`
  ([docs/mcp.md](docs/mcp.md)). PenguWatch speaks the protocol itself: no new runtime dependency.

### Changed
- **`config.yaml` is read once.** On the first start of this version the app stores the settings in your `config.yaml`
  in its database, and from then on ignores them: the file keeps only `server.host`, `server.port` and `database.db_path`.
  The built-in list of streaming services moved out of `config.example.yaml` (it is packaged with the app).
- The default for `optimizer.max_active_services` is 3 (it was 1; `config.example.yaml` already said 3). An existing
  install keeps the value it ran with.
- A `PENGUWATCH_*` variable that pins a setting (`PENGUWATCH_TZ`, `PENGUWATCH_LOG_LEVEL`, `PENGUWATCH_NOTIFY_HOUR`,
  `PENGUWATCH_NOTIFY_KIND`, `PENGUWATCH_MCP_ENABLED`, `PENGUWATCH_GRAFANA_URL`) but holds an invalid value is ignored: the
  setting's stored value (or its default) applies and stays editable, one warning in the log names the variable and why
  (never the value), the Settings tab says so next to the setting, and `penguwatch doctor` lists it. The app still
  starts (0.15 fell back to UTC or `info` for a bad time zone or log level). `PENGUWATCH_NOTIFY_KIND` and
  `PENGUWATCH_NOTIFY_HOUR` have no default any more, so they pin the setting only when set.
- **`config.yaml` is lenient outside the bootstrap keys.** A bad `server.host`, `server.port` or `database.db_path` still
  stops the start; any other value the app refuses (an unknown driver or theme, a number out of range) is left out with
  one warning naming the key and why, its default runs, and the one-time import lists it under Settings > About.
- **Webhooks and the daily digest never call this machine or a link-local address.** Every call checks the address
  the name resolves to and refuses loopback (`localhost`, `127.0.0.1`, `::1`), `0.0.0.0`, link-local (`169.254.x.x`,
  `fe80::`) and multicast, also for `PENGUWATCH_NOTIFY_URL` and webhooks imported from `config.yaml`. LAN addresses are
  fine. A receiver or ntfy server on the same host must be reached by the host's LAN address; in a rootless Podman
  container `host.containers.internal` can be link-local and is then refused too.
- **Webhook errors are coarse.** The System tab, `GET /api/webhooks` and `/metrics` describe a failed call as
  `delivered`, `redirect not followed`, `refused by the receiver (HTTP 4xx)`, `server error (HTTP 5xx)`, `timed out` or
  `could not connect`: no status code, exception name or receiver text (they could repeat an address).
- **The daily digest no longer uses a proxy.** Like the webhook client already did, its HTTP client ignores the proxy and
  certificate variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`), so a proxy can't route a call around
  the address check; the ntfy server or receiver must be reachable directly.
- **A webhook in `config.yaml` that signs with another variable or file is not imported.** The one-time import keeps a
  `secret_env` only when it is named `PENGUWATCH_WEBHOOK_...` and a `secret_file` only under `/run/secrets/`; any other
  leaves that webhook out (it would send unsigned), named in Settings > About. Rename the variable, or add the webhook
  again on the tab with a stored signing secret.
- **Compose files.** `docker-compose.yml` and `compose.release.yaml` pass `PENGUWATCH_LOG_LEVEL`, `PENGUWATCH_NOTIFY_KIND` and
  `PENGUWATCH_NOTIFY_HOUR` as `${NAME:-}` (empty, which counts as unset) instead of `info`, `ntfy` and `9`.
- The header's light/dark pick moved from `localStorage` `penguwatch.theme` to `penguwatch.mode`; the first page load keeps an
  existing pick.
- Tonight's Pick keeps its choosing rules in one place (`penguwatch.pick`), shared with the MCP tool; the endpoint's
  behaviour is unchanged.
- The test container runs as an unprivileged user with a read-only root filesystem, dropped capabilities and size limits.

### Upgrading from 0.15
- **Your `config.yaml` is imported once, then ignored** (except the listen address and the database location). Every
  setting it sets is stored with the same value, so nothing changes in behaviour; the one default that moved,
  `optimizer.max_active_services` (1 to 3), is stored as `1` for a file that did not set it. Webhooks move over with their
  addresses going to `secrets.json`; your `services:` list seeds the Services tab; your own rating sources become ordinary
  ones you can edit; a SIMKL client ID or token in the file moves into the stored login. Check **Settings > About** for
  anything the import skipped. Editing `config.yaml` later changes nothing; use the Settings tab (or Import).
- **Environment variables that pin a setting.** A set variable still wins, and the tab shows it read-only: `PENGUWATCH_TZ`
  (time zone), `PENGUWATCH_LOG_LEVEL`, `PENGUWATCH_NOTIFY_KIND`, `PENGUWATCH_NOTIFY_HOUR`, `PENGUWATCH_NOTIFY_URL` (the digest
  address), `PENGUWATCH_MCP_ENABLED` and `PENGUWATCH_GRAFANA_URL`. Remove a variable (and restart) to edit its setting on the
  tab. **If you keep your own copy of `compose.release.yaml` or `docker-compose.yml`,** take the new file or change the
  `PENGUWATCH_LOG_LEVEL`, `PENGUWATCH_NOTIFY_KIND` and `PENGUWATCH_NOTIFY_HOUR` lines to `${NAME:-}`: the old defaults
  (`info`, `ntfy`, `9`) would pin those three settings.
- **Back up `secrets.json`** (in the data folder, `/app/data` in the container) together with the database. The
  database download (`/api/export/backup.db`) deliberately does not include it. `penguwatch backup` and `scripts/deploy.sh`
  do.
- **Going back to 0.15** works on the migrated database but reads `config.yaml` again and does not see changes made on the
  tab; restore the backup you took before upgrading to return cleanly.

### Fixed
- On a window about 1280 px wide the tab bar no longer cuts off its last tabs: it shows the short names (Plan, Pick, Schedule...)
  when the full ones do not fit, and only icons below about 1100 px. It measures, so it also adapts to the theme and font.
- Searching for `%` or `_` finds titles that contain those characters instead of matching everything.
- The SIMKL account details are not fetched again for five minutes after a failed fetch, so a page that polls the sync
  status does not call SIMKL on every poll while it is unreachable.

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

[Unreleased]: https://github.com/pengupowered/penguwatch/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/pengupowered/penguwatch/compare/v0.15.2...v0.16.0
[0.15.2]: https://github.com/pengupowered/penguwatch/compare/v0.15.1...v0.15.2
[0.15.1]: https://github.com/pengupowered/penguwatch/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/pengupowered/penguwatch/releases/tag/v0.15.0
