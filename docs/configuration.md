# Configuration

PenguWatch reads two things at start:

- **`config.yaml`**: what the app should do (sync schedule, optimizer weights, household, streaming services). Copy
  `config.example.yaml` to `config.yaml` and edit it. `docker-compose.yml` mounts it read-only into the container.
  Without a `config.yaml` the app uses `config.example.yaml`.
- **Environment variables** (usually from `.env`, see `.env.example`): secrets and deployment settings. They override
  the matching `config.yaml` values. Empty values count as unset. A native install (the `penguwatch` command or its
  background service) reads them from `penguwatch.env` next to `config.yaml`, or from the file named by
  `PENGUWATCH_ENV_FILE`; see [Install](install.md#settings-and-secrets).

Restart PenguWatch after changing either: `docker compose up -d` for a container, or restart the background service
([Install](install.md#settings-and-secrets) lists the commands). The compose files pass the container only the variables
they list; to set one that is not listed (`PENGUWATCH_MAX_BODY_BYTES`, say), add it under `environment:` in a
`docker-compose.override.yml`.

## config.yaml

### server

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Address to listen on. The `penguwatch` command and the background service listen on `127.0.0.1` (this computer only) unless this key, `PENGUWATCH_HOST` or `--host` says otherwise |
| `port` | `8080` | Port to listen on |
| `log_level` | `info` | `debug`, `info`, `warning` or `error` |
| `timezone` | empty | IANA zone for household-day decisions (see [Time zone](#time-zone)) |

### database

| Key | Default | Meaning |
|---|---|---|
| `db_path` | `data/penguwatch.db` | SQLite file, relative to the working directory (`/app` in the container, so inside the data volume). The `penguwatch` command ignores this default and keeps the database in its data folder (`penguwatch paths`); set an absolute path to move it |

### drivers

Which driver fills each role. A *driver* is the code for one service (docs/drivers.md); `simkl` (the library service: an account),
`local` (a library without an account) and `tvmaze` (a catalogue and schedule that need none) ship, and every role uses
`simkl` when this section is left out. Account-free TV: `catalog: [tvmaze, simkl]` looks titles up at TVmaze first.

| Key | Default | Meaning |
|---|---|---|
| `library` | `simkl` | Where your library lives. `simkl`: an account your library is kept in step with (login, pull, push). `local`: no account, nothing to sync; your library, watch history and ratings live only in PenguWatch. One driver. |
| `catalog` | `[simkl]` | Where titles, episode lists and sequels come from; a list, tried in order (the first that knows the title wins). Needs at least one driver: it is how titles get added. |
| `schedule` | `[simkl]` | Where upcoming episodes come from; a list. `[]` turns the feature off: the Air Schedule tab and the calendar feed show a note instead. |
| `discovery` | `[simkl]` | Trending titles and recommendations; a list. `[]` turns the Discover tab off. |

```yaml
drivers:
  library: local      # no account; titles still come from the catalogue
  schedule: []        # no Air Schedule, no calendar feed
  discovery: []       # no Discover tab
```

An unknown driver, or one that cannot fill the role (`local` as a `catalog`, say), or an empty `catalog`, stops the
start with a message naming the key and the driver. Switching `library` between `simkl` and `local` loses nothing:
see [Drivers](drivers.md#switching-the-library-driver).

### tvmaze

The TVmaze driver's options (no account or key; [Drivers](drivers.md#tvmaze)). Only used when `drivers:` names `tvmaze`.

| Key | Default | Meaning |
|---|---|---|
| `rate_limit_calls` | `18` | Requests allowed per `rate_window_seconds`, 1 to 20 (TVmaze allows at least 20 per 10 seconds per address) |
| `rate_window_seconds` | `10` | The window of the limit above, up to 60 |
| `timeout_seconds` | `20` | Per request, up to 120 |
| `schedule_max_titles` | `60` | How many library titles the schedule checks per run (one request each), 1 to 500 |

### simkl

The SIMKL driver's options ([Drivers](drivers.md); [Sync](sync.md#simkl) explains the intervals). Only used when `drivers:` names `simkl`, which is the default.

| Key | Default | Meaning |
|---|---|---|
| `client_id` | empty | Your SIMKL app's client ID. Usually entered in the web UI instead, where it is stored in the database. |
| `access_token` | empty | Only to pre-seed a login; normally the Device PIN login in the UI provides it |
| `auto_sync_interval_minutes` | `30` | How often to check SIMKL for changes while the app is in use |
| `idle_after_hours` | `12` | After this long without use, check only every `idle_sync_interval_minutes` |
| `idle_sync_interval_minutes` | `180` | Check interval while idle; the next real use triggers a sync at once |
| `app_name` | `PenguWatch` | Sent to SIMKL in the `User-Agent` header |

### household

```yaml
household:
  members: ["Me", "Partner"]
```

One or two names (each 1 to 40 characters). With two, every title has a "who wants to watch" field (either person, or
both), the library and Up Next can be filtered by it, Tonight's Pick can pick for one of you, and titles both of you want
count more in the optimizer and planner. With one name those controls are hidden.

The names are only shown in the page; the database stores neutral keys (`person1`, `person2`, `both`), so renaming
someone later keeps every title's tag.

### optimizer

| Key | Default | Meaning |
|---|---|---|
| `max_active_services` | `1` (`3` in the example) | How many paid services the rotation planner may keep at once |
| `household_priority_boost` | `2.0` | Weight of "both want it" titles in a service's score |
| `ready_to_binge_boost` | `1.5` | Weight of shows with a complete season waiting |

### services

The streaming services the app knows about, and which networks each one carries:

```yaml
services:
  - id: "netflix"                 # letters, digits, . _ - (stable: titles refer to it)
    name: "Netflix"
    monthly_cost: 19.99           # monthly equivalent, also for yearly plans
    is_active: false              # subscribed right now
    aliases: ["Netflix", "The CW"]   # network names the catalogue reports for titles this service carries
    titles: ["Seinfeld"]          # exact titles it carries whatever their network says (licensing exceptions)
  - id: "disney_hulu_noads"
    name: "Disney+ & Hulu bundle (no ads)"
    monthly_cost: 21.99
    includes: ["disney_plus", "hulu"]   # a bundle: its members cost nothing extra while it is active
    aliases: ["Disney+ Hulu Bundle"]
```

Optional keys: `renewal_day` (1-31) or `renewal_date` (`YYYY-MM-DD`), `billing_period` (`monthly` or `yearly`) and
`notes`.

On every start, each service's `name`, `aliases`, `titles` and `includes` are updated from `config.yaml`. Its price, whether it is
active, its renewal date and its notes are yours: they are taken from `config.yaml` only when the service is new, and
after that you change them in the app (Services & Sync). Services you add in the app are kept even though they are not
in `config.yaml`. The example's prices are US list prices for an ad-free tier at the time of writing; check yours.

A service list that is missing from `config.yaml` means no services are seeded, so keep the `services:` block when you
trim the file.

#### Which service carries a title

A new title gets a service automatically, from your `services:` list only, in this order:

1. **Title rules:** the title is listed in a service's `titles` (exact title, case ignored).
2. **Exact network:** the network the catalogue reports equals a service's `name` or one of its `aliases`.
3. **Network contains an alias** as whole words ("FX on Hulu" contains "FX", "Cinemax" does not contain "Max"); the
   longest such alias wins, so "BBC Two London" picks the service listing "BBC Two" over one listing "BBC".
4. **An alias contains the network** as whole words ("Smithsonian" in "Smithsonian Channel"); the shortest (closest) one wins.

On a tie the service listed first wins. An alias shorter than two characters only matches exactly.

When nothing matches, the title has no service (anime falls back to `crunchyroll` if you have it). The example's
aliases and title rules describe US streaming; where you live, a network may stream elsewhere: edit the aliases, add
`titles`, or pick the right service under **Streams on** in the title's details. Manual picks are remembered and never
overwritten by syncs. **Tidy** lists the titles that have no service yet.

### ratings

Your own rating sources (reviews you keep elsewhere): see [Extending](extending.md#bring-your-own-ratings).

```yaml
ratings:
  sources:
    - id: "letterboxd"      # lowercase letters, digits, - and _ (1 to 32 characters); the built-in ids (simkl, imdb, tvmaze), mal, user, my and your are taken
      name: "Letterboxd"    # up to 60 characters
      label: "LB"           # up to 8 characters, for the badge
      scale_min: 0          # default 0; must be below scale_max
      scale_max: 5          # default 10
      decimals: 1           # shown decimals, 0 to 3 (default 1)
      show_on_cards: true   # a badge on the library cards (default false)
```

On every start the sources listed here are created or brought in line with the file (the app shows them read-only);
their values are never touched. A source you remove from the file keeps its values and becomes one you can edit in the
app. Sources can also be added in the app and through the API without touching this file.

### webhooks

Calls to your own URLs when something happens (a title is added, a status changes, an episode is watched, ...): the
events, payloads and signature are in [Extending](extending.md#event-webhooks).

```yaml
webhooks:
  - name: "home-automation"       # optional; shown on the System tab (default: the host of the url)
    url: "https://hooks.example.net/penguwatch"
    events: ["title.added", "sync.failed"]   # optional; default: all events
    secret_env: "PENGUWATCH_WEBHOOK_SECRET"  # optional: the NAME of an environment variable that holds the signing secret
    # secret_file: "/run/secrets/penguwatch_webhook"   # or a file that holds it
```

At most ten webhooks. `name` is 1 to 40 letters, digits, spaces, `.`, `_` or `-`; use `secret_env` or `secret_file`, not both. `url` must be `http://` or `https://` with a valid port; `secret_env` is an UPPER_CASE variable name (letters, digits, `_`, at most 64 characters). The secret (at least 16 characters) is never written in
this file; a webhook whose `secret_env` or `secret_file` cannot be read stays off and the System tab says why. Pass the
variable to the container like any other (an `environment:` entry or `.env`).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PENGUWATCH_HOST`, `PENGUWATCH_PORT` | `0.0.0.0`, `8080` (`127.0.0.1` with the `penguwatch` command) | Override `server.host` / `server.port` |
| `PENGUWATCH_LOG_LEVEL` | `info` | Overrides `server.log_level` |
| `PENGUWATCH_LOG_FORMAT` | `text` | `json` writes one JSON object per line, with the request id |
| `PENGUWATCH_DB_PATH` | | Overrides `database.db_path` |
| `PENGUWATCH_CONFIG` | | The config file to read instead of searching (`--config` of the `penguwatch` command) |
| `PENGUWATCH_ENV_FILE` | `penguwatch.env` next to the config | The settings file the `penguwatch` command loads (NAME=value lines; an already set variable wins) |
| `PENGUWATCH_DATA` | | The data folder of the `penguwatch` command (`--data-dir`); the container uses `/app/data` |
| `PENGUWATCH_TZ` | | Overrides `server.timezone` |
| `PENGUWATCH_ACCESS_TOKEN` | | Requires this token for every page and API call (see [Security](security.md)) |
| `PENGUWATCH_ALLOWED_HOSTS` | | Extra host names the app may be reached by, comma separated; `.example.net` matches subdomains |
| `PENGUWATCH_ALLOWED_ORIGINS` | | Extra browser origins allowed to make changes (a reverse proxy with another public name) |
| `PENGUWATCH_COOKIE_SECURE` | | `1` when served over HTTPS, so the sign-in cookie is marked `Secure` |
| `PENGUWATCH_SESSION_MAX_AGE_DAYS` | `30` | How long a browser sign-in lasts (at least 0.0417, one hour; at most 400) |
| `PENGUWATCH_MAX_BODY_BYTES` | `262144` | Largest accepted request body |
| `PENGUWATCH_NOTIFY_URL` | | ntfy topic URL or JSON webhook for the daily digest (see [Operations](operations.md)) |
| `PENGUWATCH_NOTIFY_KIND` | `ntfy` | `ntfy`; any other value posts a JSON webhook (see [Operations](operations.md#notifications)) |
| `PENGUWATCH_NOTIFY_HOUR` | `9` | Local hour of the daily check |
| `PENGUWATCH_GRAFANA_URL` | | Grafana base URL; adds Dashboard / Logs / Traces links to the header and the System tab |
| `SIMKL_CLIENT_ID`, `SIMKL_ACCESS_TOKEN` | | Override `simkl.client_id` / `simkl.access_token` (the SIMKL driver's own variables; TVmaze has none) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` and the other standard `OTEL_*` variables | | OpenTelemetry export (see [Operations](operations.md)) |

## Time zone

Decisions that depend on "what day is it for the household" (renewal countdowns, "aired today", the hour of the daily
notification) use `PENGUWATCH_TZ`, else `server.timezone`, else the `TZ` variable, else UTC. Stored timestamps are
always UTC.
