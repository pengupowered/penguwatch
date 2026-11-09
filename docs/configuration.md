# Configuration

PenguWatch is configured in three places:

- **The Settings tab** is the way to change how the app behaves: household names, sync schedule, optimizer weights,
  the daily digest and webhooks, theme and time zone, data sources, and the advanced values. Changes apply at once
  (a few say "Applies after a restart") and are kept in the database, with a history you can restore from. Streaming
  services have their own tab (Services & Sync) and your own rating sources are on the Ratings tab.
- **`config.yaml`** keeps only the bootstrap keys: where the app listens and where its database is. Nothing else in it
  matters after the first start (see [The one-time import](#the-one-time-import)).
- **Environment variables** (usually from `.env`, see `.env.example`): secrets and deployment settings, and a few
  variables that pin a setting (see [Environment variables](#environment-variables)). Empty values count as unset. A
  native install (the `penguwatch` command or its background service) reads them from `penguwatch.env` next to
  `config.yaml`, or from the file named by `PENGUWATCH_ENV_FILE`; see [Install](install.md#settings-and-secrets).

Restart PenguWatch after changing `config.yaml` or an environment variable: `docker compose up -d` for a container, or
restart the background service ([Install](install.md#settings-and-secrets) lists the commands). The compose files pass
the container only the variables they list; to set one that is not listed (`PENGUWATCH_MAX_BODY_BYTES`, say), add it
under `environment:` in a `docker-compose.override.yml`. Settings changed on the tab need no restart, except the ones
marked in the table below.

## config.yaml: the bootstrap keys

| Key | Default | Meaning |
|---|---|---|
| `server.host` | `0.0.0.0` | Address to listen on. The `penguwatch` command and the background service listen on `127.0.0.1` (this computer only) unless this key, `PENGUWATCH_HOST` or `--host` says otherwise |
| `server.port` | `8080` | Port to listen on |
| `database.db_path` | `data/penguwatch.db` | SQLite file, relative to the working directory (`/app` in the container, so inside the data volume). The `penguwatch` command ignores this default and keeps the database in its data folder (`penguwatch paths`); set an absolute path to move it |

These are read at every start, because the app needs them before it can open the database. `config.example.yaml`
contains them and, for the first start only, the values listed in the next section. You may delete everything but
these keys (or the whole file, when the defaults suit you; a container still needs the file to exist, because compose
mounts it).

## The one-time import

On the first start of a version with the Settings tab, PenguWatch reads your `config.yaml` once and stores what it
finds in the database:

- every setting in the table below that the file sets (a value the file sets to its default is stored too);
- the one value whose default changed: `optimizer.max_active_services` was `1` when the file left it out and is `3`
  now. A file that does not set it gets `1` stored, so the plan stays as it was; a new install gets `3`;
- webhooks, with their addresses moved to the secrets file (see [Secrets](#secrets));
- your `services:` list, which seeds the Services tab (a file without one gets the built-in list of 14 US services);
- your `ratings: sources:` list, which become ordinary sources you can edit on the Ratings tab;
- a SIMKL `client_id` and `access_token`, moved into the stored login when none is stored yet.

A value the file has wrong (a typo, out of range) is skipped and reported on the Settings tab under About, never
stopping the start. Environment variables are not copied: a variable that is set keeps winning.

After the import **`config.yaml` is ignored for settings.** Editing it changes nothing but the bootstrap keys. If it
still holds different values, the start-up log and the Settings tab's About box say how many keys differ (names only).
To apply values from a YAML file later, use **Import** on the Settings tab: it shows what would change and applies
nothing until you save. To check what the one-time import would do before upgrading, run `penguwatch settings
check-import CONFIG DB` with your `config.yaml` and a copy of your database (for example from `penguwatch backup`): it
imports into a temporary copy, never writes `DB`, and lists what would be stored, skipped or differ, never a value.
`--live` compares a database that was already imported with the file.

A version that predates the Settings tab (0.15 and earlier) reads `config.yaml` again if you go back to it, and does not
see changes made on the tab; restore the database backup you took before upgrading to go back cleanly
([Operations](operations.md#running-and-updating)).

## The Settings tab

Sections are the tab's own: Household, Sync, Optimizer, Notifications, Display, Integrations, Data sources and Advanced.
Each setting shows its range, what a change does and, when set by an environment variable, "From environment" and the
variable's name; that setting is read-only until the variable is removed and the app restarted. **Reset** returns a
setting to its default. Every save is kept as a version (the last 50); **Versions** shows what each changed and restores
one as a new save. **Export** downloads the settings as a YAML file in `config.yaml` syntax, without any address or
secret; **Import** reads such a file into the form for you to review.

Two people saving at once cannot overwrite each other: a save that touches something changed meanwhile is refused with
"Changed elsewhere since you opened the page", and the tab reloads.

Without `PENGUWATCH_ACCESS_TOKEN`, anyone who can reach the port can change these settings, including where the daily
digest and the webhooks are sent. The tab says so at the top. See [Security](security.md).

### All settings

"Live" settings apply as soon as you save; "restart" ones are stored at once and apply at the next start (the tab shows
a note until then). The key is the name used by Export, Import and the API.

| Section | Setting | Key | Default | Range | Applies | Env override |
|---|---|---|---|---|---|---|
| Household | Who watches | `household.members` | `Me`, `Partner` | 1 to 20 people (in effect, no limit), a name each of 1 to 40 characters, no two alike | live (reloads the page) | |
| Sync (SIMKL) | Check for changes every | `simkl.auto_sync_interval_minutes` | `30` min | 5 to 1440 | live | |
| Sync (SIMKL) | Quiet after | `simkl.idle_after_hours` | `12` h | 1 to 168 | live | |
| Sync (SIMKL) | Check while quiet every | `simkl.idle_sync_interval_minutes` | `180` min | 15 to 1440, not below the check above | live | |
| Optimizer | Services kept at once | `optimizer.max_active_services` | `3` | 1 to 10 | live | |
| Optimizer | Weight of shared titles | `optimizer.household_priority_boost` | `2.0` | 0 to 10, per person beyond the first | live | |
| Optimizer | Weight of complete seasons | `optimizer.ready_to_binge_boost` | `1.5` | 0 to 10 | live | |
| Notifications (Daily digest) | Send the digest as | `notify.kind` | `ntfy` | `ntfy` or `webhook` | live | `PENGUWATCH_NOTIFY_KIND` |
| Notifications (Daily digest) | Check every day at | `notify.hour` | `9` | 0 to 23 (local hour) | live | `PENGUWATCH_NOTIFY_HOUR` |
| Notifications (Daily digest) | Warn before a renewal | `notify.renewal_warning_days` | `3` days | 1 to 14 | live | |
| Display | Theme for new browsers | `display.theme` | `default` | a theme id ([Themes](themes.md)) | live (next page load) | |
| Display | Theme animation | `display.effects` | on | on or off | live (next page load) | |
| Display | Time zone | `server.timezone` | empty (the server's zone: `TZ`, else UTC) | empty or an IANA name | live | `PENGUWATCH_TZ` |
| Integrations | Allow AI assistants to read (MCP) | `mcp.enabled` | off | on or off | live | `PENGUWATCH_MCP_ENABLED` |
| Integrations | Grafana address | `display.grafana_url` | empty (no links) | a plain `http(s)://` base URL | live (next page load) | `PENGUWATCH_GRAFANA_URL` |
| Data sources | Library | `drivers.library` | `simkl` | one driver that can sync a library | restart | |
| Data sources | Catalogue | `drivers.catalog` | `simkl` | one or more drivers, tried in order | restart | |
| Data sources | Schedule | `drivers.schedule` | `simkl` | any number, none turns the feature off | restart | |
| Data sources | Discovery | `drivers.discovery` | `simkl` | any number, none turns Discover off | restart | |
| Advanced (Optimizer) | Viewing window | `optimizer.value_window_days` | `90` days | 30 to 365 | live | |
| Advanced (Optimizer) | Not used below | `optimizer.unused_hours` | `0.5` h | 0 to 20 | live | |
| Advanced (History) | Bulk mark from | `history.bulk_min` | `3` episodes | 2 to 20 | live | |
| Advanced (Logging) | Log level | `server.log_level` | `info` | `debug`, `info`, `warning`, `error` | live | `PENGUWATCH_LOG_LEVEL` |
| Advanced (SIMKL) | App name sent to SIMKL | `simkl.app_name` | `PenguWatch` | 1 to 40 printable ASCII characters | restart | |
| Advanced (TVmaze) | Titles checked per schedule run | `tvmaze.schedule_max_titles` | `60` | 1 to 500 | live | |
| Advanced (TVmaze) | Requests per window | `tvmaze.rate_limit_calls` | `18` | 1 to 20 | restart | |
| Advanced (TVmaze) | Rate window | `tvmaze.rate_window_seconds` | `10` s | above 0 to 60 | restart | |
| Advanced (TVmaze) | Request timeout | `tvmaze.timeout_seconds` | `20` s | above 0 to 120 | restart | |

The SIMKL rows (the Sync section, and the app name under Advanced) matter only while SIMKL is one of your drivers, and
the TVmaze rows under Advanced only while TVmaze is; the tab shows them either way.

Besides these, the tab holds a **list** and the **secrets**:

- **Event webhooks** (Notifications): up to ten calls to your own URLs when something happens; see
  [Webhooks](#webhooks) below.
- **Secrets** (write-only): the digest address (`notify.url`, an ntfy topic URL or a JSON webhook), and each webhook's
  address and signing secret. See [Secrets](#secrets).

### What the settings mean

**Household.** 1 to 20 people (20 is a guard, not a plan: in effect there is no limit). On the tab each person is a row
with a Remove button, and **Add person** adds one. With two or more, every title says who wants to watch it (any of
them, ticked per person), the Library and Up Next can be filtered by one person's own titles or the shared ones,
Tonight's Pick can pick for one of you or from the shared titles, and a title counts more the more people want it (the
optimizer, the planner, the likely-interest score and Tonight's Pick). With one person those controls are hidden.

Each person has an id (`person1`, `person2`, ...) that titles store; the names are only shown in the page. Renaming
someone keeps their id, so every title keeps its people. A new person gets the next unused id; an id is never given to
anyone else, even after its person is removed. Removing someone takes them off their titles, and a title that was only
theirs goes to the first person: the tab says how many before you save. In `config.yaml` (read once, see above) and in
an exported file the setting is a list of `{id, name}`; a plain list of names, as before 0.17, still reads, as
`person1`, `person2`, ... in order.

```yaml
household:
  members:
    - {id: person1, name: Ann}
    - {id: person2, name: Bob}
    - {id: person4, name: Cid}   # person3 was removed: its id is not used again
```

**Sync.** [Sync](sync.md#simkl) explains the intervals: while the app is in use it checks SIMKL every
`auto_sync_interval_minutes`; after `idle_after_hours` without use it checks only every `idle_sync_interval_minutes`,
and the next real use triggers a sync at once.

**Optimizer.** `max_active_services` is how many paid services the rotation planner may keep at once; the two weights
raise a service's score for shared titles (the weight counts once per person beyond the first who wants a title, so
with two people it is exactly the "both want" weight it was) and for shows with a complete season waiting. The viewing window and
the "not used" threshold (Advanced) decide which services count as used.

**Display.** The theme every browser shows until its person picks one in the header's theme menu: `default`,
`amber-crt`, `blueprint`, `catppuccin`, `cinema`, `dracula`, `e-ink`, `gruvbox`, `high-contrast`, `nord`, `one-dark`,
`rose-pine`, `solarized`, `starship-console`, `synthwave`, `tokyo-night` or `video-store` (see [Themes](themes.md)).
Light or dark is a separate choice, made in the same menu. A saved theme must be one of these. **Theme animation** is
whether a theme's subtle background animation (Amber CRT, Cinema, Starship console, Synthwave and Video store; the
palette themes have none) runs in a browser whose person has not turned it on or off in the header menu's "Animation"
item; that browser's own choice wins, and nothing runs while the system asks for reduced motion ([Themes:
Effects](themes.md#effects)). The time zone is for decisions that depend on "what day is it for the household" (renewal
countdowns, "aired today", the hour of the daily digest, History's charts); [Time zone](#time-zone) gives the order of
precedence. Stored timestamps are always UTC.

**MCP.** Serves the read-only MCP endpoint (`POST /mcp`) that lets an AI assistant ask about your library. Off by
default; while it is off `/mcp` answers 404. It is guarded like the API: with `PENGUWATCH_ACCESS_TOKEN` set it needs the
same bearer token, without one it is open (saving with MCP on and no token shows a warning). See [MCP](mcp.md).

**Data sources.** Which driver fills each role. A *driver* is the code for one service ([Drivers](drivers.md));
`simkl` (an account), `local` (a library without an account) and `tvmaze` (a catalogue and schedule that need none)
ship, and every role uses `simkl` by default. Account-free TV: library `local`, catalogue `tvmaze`, schedule `tvmaze`,
discovery none. The tab offers only drivers that can fill the role, and refuses an empty catalogue (it is how titles get
added). Roles apply after a restart. Switching the library between `simkl` and `local` loses nothing: see
[Drivers](drivers.md#switching-the-library-driver).

**TVmaze** (Advanced). The TVmaze driver's options ([Drivers](drivers.md#tvmaze)); no account or key. They matter only
when TVmaze is one of your drivers. TVmaze allows at least 20 requests per 10 seconds per address.

### Webhooks

Calls to your own URLs when something happens (a title is added, a status changes, an episode is watched, ...): the
events, payloads and signature are in [Extending](extending.md#event-webhooks). On the tab, each webhook has a name (it
gives the webhook its id, for example `Home Assistant` becomes `home-assistant`, fixed once saved), the events it wants
(default: all), its address and a signing secret.

At most ten webhooks. The address must be `http://` or `https://` with a host and a valid port, without
`user:password@`, and must not be this machine or a link-local address: `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`,
`169.254.x.x` and numeric spellings of them (`2130706433`) are refused, and the same check runs on the resolved address
at every call, so a name that points at this machine is refused too. Addresses on your LAN (`192.168.x.x`, `10.x.x.x`)
are fine. Run the receiver on another device, or reach it by the LAN address of this one. The signing secret is at
least 16 characters, typed on the tab, and never shown again.

A webhook imported from a 0.15 `config.yaml` may read its secret from an environment variable (`secret_env`, named
`PENGUWATCH_WEBHOOK_...`) or a file (`secret_file`, under `/run/secrets/`); the import leaves out a webhook that names
any other variable or file, and lists it under About. The tab shows such a variable or file read-only, with **Stop
using it**, which switches the webhook to a typed secret (set it in the same save). The tab cannot name a new variable
or file, and while a webhook reads one its address can't be changed there: otherwise anyone who can reach the page
could have that secret sign calls to an address of their choosing
([Security](security.md#settings-and-secrets)). A webhook whose variable or file cannot be read stays off and the
System tab says why.

A webhook with no address stays off and the tab says "address needed".

### Secrets

The digest address and each webhook's address and signing secret are secrets: an ntfy topic URL is a password to your
notifications. They are kept in **`secrets.json`**, a file next to the database (`/app/data/secrets.json` in the
container; `penguwatch paths` shows the location for a native install), readable by you only (mode 0600) and written
atomically. They are **write-only**: no page, API answer, Export, version, log line or error message ever shows one; the
tab shows "set" and the address's host. Enter a new value to replace one, or clear it to remove it. An address (the digest's or a
webhook's) follows the rules under [Webhooks](#webhooks): no `user:password@` (ntfy takes an access token as
`?auth=...` instead), and not this machine or a link-local address, also when it comes from `PENGUWATCH_NOTIFY_URL`. They are not in
the database, so **back up `secrets.json` together with the database** ([Operations](operations.md#backups)); the
database download on the System tab does not include it.

`PENGUWATCH_NOTIFY_URL`, when set, wins over the stored digest address (see below). The SIMKL login is not in this file:
it stays in the database, where it always was.

## services and ratings

Streaming services and your own rating sources are not part of the settings table; they have their own tabs.

**Services** (Services & Sync tab): each has an id (letters, digits, `.`, `_`, `-`, stable: titles refer to it), a name,
a monthly price (the monthly equivalent, also for yearly plans), whether it is active, optionally a renewal day or date,
a billing period and notes, the network names the catalogue reports for titles it carries (`aliases`), exact titles it
carries whatever their network says (`titles`) and, for a bundle, the services it includes (`includes`, which cost
nothing extra while the bundle is active). The first start of this version seeds the table from your `config.yaml`, or
from the built-in list. Prices and renewal dates are yours and never overwritten.

Services that came from the built-in list and that you never edited are brought up to date with the built-in list at
every start (name, aliases, titles and bundle members), so a release that corrects an alias reaches you. Editing means
changing one of those four fields on the Services tab (Edit) or with `POST /api/services`; a price, renewal date, note,
billing period or switching a service on or off does not count, since those are never updated anyway. A service you
edited, added yourself, or imported from a `config.yaml` list where it differs from the built-in entry is never touched
again. The tab does not mark which services are edited. The built-in list describes US streaming: edit the aliases for
your country, add `titles`, or pick the right service under **Streams on** in a title's details. New services in a later
built-in list are not added to an existing install.

#### Which service carries a title

A new title gets a service automatically, from your services only, in this order:

1. **Title rules:** the title is listed in a service's `titles` (exact title, case ignored).
2. **Exact network:** the network the catalogue reports equals a service's `name` or one of its `aliases`.
3. **Network contains an alias** as whole words ("FX on Hulu" contains "FX", "Cinemax" does not contain "Max"); the
   longest such alias wins, so "BBC Two London" picks the service listing "BBC Two" over one listing "BBC".
4. **An alias contains the network** as whole words ("Smithsonian" in "Smithsonian Channel"); the shortest (closest) one wins.

On a tie the service listed first wins. An alias shorter than two characters only matches exactly.

When nothing matches, the title has no service (anime falls back to `crunchyroll` if you have it). Manual picks are
remembered and never overwritten by syncs. **Tidy** lists the titles that have no service yet.

**Rating sources** (Ratings tab): reviews you keep elsewhere; see [Extending](extending.md#bring-your-own-ratings). A
source has an id (lowercase letters, digits, `-` and `_`, 1 to 32 characters; the built-in ids `simkl`, `imdb`,
`tvmaze`, and `mal`, `user`, `my`, `your` are taken), a name (up to 60 characters), a badge label (up to 8), a scale
(`scale_min` below `scale_max`, default 0 to 10), shown decimals (0 to 3) and whether it shows on the library cards.
Sources listed in a 0.15 `config.yaml` were read-only; after the import they are ordinary sources you can edit.

## Environment variables

A **pinning** variable (marked below) fixes its setting: while it is set to a usable value, the setting shows "From
environment" and is read-only, and a save that touches it is refused. Remove the variable and restart to edit the setting
on the tab; the stored value (if any) comes back. A pinning variable with a value the setting does not accept (an hour of
`25`) is ignored with one warning in the log and on the tab, and the stored value applies. Variables left empty count as
unset, which is why the compose files pass them as `${NAME:-}`.

| Variable | Default | Meaning |
|---|---|---|
| `PENGUWATCH_HOST`, `PENGUWATCH_PORT` | `0.0.0.0`, `8080` (`127.0.0.1` with the `penguwatch` command) | Override `server.host` / `server.port` |
| `PENGUWATCH_LOG_LEVEL` | | **Pins** `server.log_level` (`debug`, `info`, `warning`, `error`) |
| `PENGUWATCH_LOG_FORMAT` | `text` | `json` writes one JSON object per line, with the request id |
| `PENGUWATCH_DB_PATH` | | Overrides `database.db_path` |
| `PENGUWATCH_CONFIG` | | The config file to read instead of searching (`--config` of the `penguwatch` command) |
| `PENGUWATCH_ENV_FILE` | `penguwatch.env` next to the config | The settings file the `penguwatch` command loads (NAME=value lines; an already set variable wins) |
| `PENGUWATCH_DATA` | | The data folder of the `penguwatch` command (`--data-dir`); the container uses `/app/data`. `secrets.json` lives in the database's folder |
| `PENGUWATCH_TZ` | | **Pins** `server.timezone` |
| `PENGUWATCH_ACCESS_TOKEN` | | Requires this token for every page and API call (see [Security](security.md)) |
| `PENGUWATCH_ALLOWED_HOSTS` | | Extra host names the app may be reached by, comma separated; `.example.net` matches subdomains |
| `PENGUWATCH_ALLOWED_ORIGINS` | | Extra browser origins allowed to make changes (a reverse proxy with another public name) |
| `PENGUWATCH_COOKIE_SECURE` | | `1` when served over HTTPS, so the sign-in cookie is marked `Secure` |
| `PENGUWATCH_SESSION_MAX_AGE_DAYS` | `30` | How long a browser sign-in lasts (at least 0.0417, one hour; at most 400) |
| `PENGUWATCH_MAX_BODY_BYTES` | `262144` | Largest accepted request body |
| `PENGUWATCH_MCP_ENABLED` | | **Pins** `mcp.enabled` (`true` or `false`; the read-only MCP endpoint, see [MCP](mcp.md)) |
| `PENGUWATCH_NOTIFY_URL` | | **Pins** the digest address (an ntfy topic URL or JSON webhook); the tab cannot change it while this is set (see [Operations](operations.md#notifications)) |
| `PENGUWATCH_NOTIFY_KIND` | | **Pins** `notify.kind`: `ntfy`; any other value posts a JSON webhook |
| `PENGUWATCH_NOTIFY_HOUR` | | **Pins** `notify.hour` (0 to 23, local hour) |
| `PENGUWATCH_GRAFANA_URL` | | **Pins** `display.grafana_url`; adds Dashboard / Logs / Traces links to the header and the System tab |
| `PENGUWATCH_WEBHOOK_...` | | The signing secret of a webhook imported from a 0.15 `config.yaml` that names it as its `secret_env` (the tab cannot name a new one) |
| `SIMKL_CLIENT_ID`, `SIMKL_ACCESS_TOKEN` | | Pre-seed the SIMKL driver's credentials (the driver's own variables; TVmaze has none) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` and the other standard `OTEL_*` variables | | OpenTelemetry export (see [Operations](operations.md)) |

Before this version the compose files passed `PENGUWATCH_LOG_LEVEL`, `PENGUWATCH_NOTIFY_KIND` and `PENGUWATCH_NOTIFY_HOUR`
with the defaults `info`, `ntfy` and `9`, which would now pin those three settings. The current compose files pass
them empty. If you kept an older copy of `compose.release.yaml`, take the new one or change those three lines to
`${NAME:-}` ([CHANGELOG](../CHANGELOG.md)).

## Time zone

Decisions that depend on "what day is it for the household" (renewal countdowns, "aired today", the hour of the daily
notification) use `PENGUWATCH_TZ`, else the time zone on the Settings tab, else the `TZ` variable, else UTC. Stored
timestamps are always UTC.
