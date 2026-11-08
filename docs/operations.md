# Operations

## Running and updating

`docker compose up -d --build` (or `podman-compose up -d --build`) builds the image and starts the container with
`restart: always`. Data (the SQLite database, the stored tracker login, the sync outbox) lives in the named volume
`penguwatch_data` and survives rebuilds. Stopping the container sends SIGTERM, and the app delivers pending
changes before it exits (the compose file allows 20 s).

To update: `git pull`, then `docker compose up -d --build`. Database migrations run on start. Back up first (below).
Going back to an older version means restoring the backup you took before the update: an older version does not know the
settings stored in the database and reads `config.yaml` again ([Configuration](configuration.md#the-one-time-import)).

With rootless Podman, `systemctl --user enable podman-restart.service` (with `loginctl enable-linger`) brings the
container back after a reboot.

`scripts/deploy.sh` is the maintainer's deploy for rootless Podman: it runs a dependency audit with the separate
`depguard` tool, tags the running image as a rollback point, copies the database and `secrets.json` to a private backup
folder (mode 0600), rebuilds, recreates the container and waits for `/api/health`. You don't need it to run PenguWatch.

## Backups and exports

What to keep, for a complete backup: the **database** and **`secrets.json`**, both in the data folder (the
`penguwatch_data` volume, `/app/data`; `penguwatch paths` for a native install). The database holds your library, your
services, your rating sources and all the settings; `secrets.json` holds the secrets entered on the Settings tab (the
daily digest address, and each event webhook's address and signing secret). Without the file, a restored install has all
its settings but the digest and the webhooks are switched off until you enter their addresses again.

* `penguwatch backup` (native installs): copies the database and `secrets.json` (mode 0600) into a folder you name. It is
  the private full backup; it keeps the stored library login as well. `scripts/deploy.sh` does the same for the container
  before each deploy.
* `GET /api/export/backup.db`: a consistent snapshot of the database (SQLite `VACUUM INTO`) with the library service's
  credentials removed. It runs on its own connection and does not block the app. **It does not contain `secrets.json`**:
  the download is meant to be safe to keep anywhere, so the digest and webhook addresses and secrets are not in it, and
  you enter them again after restoring it. Take a copy of `secrets.json` yourself if you want them
  (`docker cp penguwatch:/app/data/secrets.json .`) and keep it as private as the `.env` file.
* `GET /api/export/library.csv`: your library as a spreadsheet, with a column per custom rating source.
* Settings > Export: the settings as a YAML file, without any address or secret ([Configuration](configuration.md#the-settings-tab)).
  Handy for moving settings to another install; it is not a backup.

The backup includes your rating sources and their values, the settings and their versions. `config.yaml` is not needed to
restore anything except the listen address and database location.

The database runs in WAL mode with `synchronous=NORMAL`: crash-safe, but the last moments before a power cut may be
lost. Writes are serialized, so one request never commits another's half-finished work.

To restore a backup, stop the container, copy the file into the volume as `data/penguwatch.db` (remove any
`penguwatch.db-wal` and `-shm` files next to it) and, if you have it, `secrets.json` next to it (mode 0600, owned by the
app's user, uid 10001 in the container), start the container and reconnect the library service's account if you use one
(a backup from `/api/export/backup.db` has no login in it; one from `penguwatch backup` keeps it). With a database
download and no `secrets.json`, open the Settings tab and enter the digest and webhook addresses again. The restored
database brings back every setting and their versions; an older `config.yaml` beside it changes nothing.

## Monitoring

* **System tab** (`#/system`, or `g` then `y`): version, uptime, sync health and history, API latency per route, upstream
  call counts and errors, database size, and a warning while no access token is set.
* **Health:** `/api/health` (open, the process is up) and `/api/health/ready` (the database answers). Point an uptime
  monitor at the first.
* **Prometheus:** `/metrics` (needs the token when one is set). Suggested alerts: sync age
  (`penguwatch_library_last_sync_age_seconds`), failing outbox items (`penguwatch_outbox_items{state="failing"}`), any
  5xx, `penguwatch_webhook_deliveries_total{outcome="failed"}` or `"dropped"` growing, and
  `penguwatch_webhook_queue_depth`.
* **Request ids:** every response carries `X-Request-ID` and `Server-Timing`, and the same id is in the log lines for
  that request. `PENGUWATCH_LOG_FORMAT=json` writes one JSON object per line.
* The page reports API calls that took two seconds or more in the browser, with the browser's timing breakdown, to the
  log, so slow phones or networks show up there.

## OpenTelemetry

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP/HTTP collector (for example `http://otel-collector:4318`) and traces,
metrics and logs are exported; without it nothing is. The standard `OTEL_*` variables apply (`OTEL_SERVICE_NAME`
defaults to `penguwatch`, `OTEL_SDK_DISABLED=true` turns export off). If the collector runs in another compose project
on the same host, join its network with a `docker-compose.override.yml` (see `docker-compose.override.example.yml`).

* **Traces:** every API request (with its request id), each call attempt to a driver's service, sync runs, pushed batches, token
  renewals and the daily notification check.
* **Metrics:** HTTP latency and status per route, upstream calls per service, endpoint and status, sync runs and duration, changes
  pushed and pulled, worker passes and how long since it last woke, event-loop lag, outbox, library, database, login
  state, memory and CPU.
* **Logs:** INFO and above, tied to their trace.
* **Never exported:** titles, search text (query values are replaced by `REDACTED`), tokens, the calendar key.
  `/login`, `/static`, the health checks and `/metrics` are not traced.

The Grafana address (Settings > Integrations, or `PENGUWATCH_GRAFANA_URL`, which pins it) adds Dashboard / Logs / Traces links to the header and the System tab. They expect a Grafana
dashboard with the uid `penguwatch` and Loki and Tempo data sources.

## Notifications

On the Settings tab, under Notifications, enter the digest address: an [ntfy](https://ntfy.sh) topic URL (or, with the
kind set to webhook, any URL that accepts a JSON POST). The address is a secret: it is stored in `secrets.json`, never
shown again and never exported. One check a day, at the hour you set there (local time, Display > Time zone), sends:

* a paid service renewing within the warning days you set (3 by default) that wasn't used,
* episodes airing today on a service you have,
* a failing sync.

`PENGUWATCH_NOTIFY_URL`, `PENGUWATCH_NOTIFY_KIND` and `PENGUWATCH_NOTIFY_HOUR` still work: a variable that is set pins that
setting, and the tab shows it read-only ([Configuration](configuration.md#environment-variables)).

The address must not be this machine or a link-local address, also when it comes from `PENGUWATCH_NOTIFY_URL`: a
self-hosted ntfy on the same host is reached by the host's LAN address, not `localhost` (in a rootless Podman container
`host.containers.internal` may be link-local and is refused too). The digest's client ignores proxy variables, and a
failed send is reported only as `refused by the receiver (HTTP 4xx)`, `server error (HTTP 5xx)`, `timed out` or `could
not connect`.

`GET /api/notify/preview` shows what would be sent; `POST /api/notify/test` sends a test message.

The daily digest stays as it is. To get a call for each event as it happens (and to build your own notifications), use
[event webhooks](extending.md#event-webhooks); their delivery state is on the System tab.
