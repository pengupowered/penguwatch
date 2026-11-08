# Drivers

A driver is the code for one service: a tracker with an account (the **library service**, the one your library is kept
in step with), a public catalogue. It turns that service's answers
into the neutral types of `src/penguwatch/drivers/base.py`, so nothing else in PenguWatch knows the service. The design
is in [Architecture](architecture.md#the-driver-layer); which driver fills which role is the Settings tab's Data sources (`drivers:`)
([Configuration](configuration.md#what-the-settings-mean)).

## The drivers that ship

| Driver | Offers | Account | Options |
|---|---|---|---|
| `simkl` | `catalog`, `library_sync`, `schedule`, `discovery`, `ratings` (SIMKL, IMDb / MyAnimeList) | a SIMKL app id, then a device-PIN login | Settings tab, Sync and Advanced ([Configuration](configuration.md#all-settings)) |
| `tvmaze` | `catalog`, `schedule`, `ratings` (TVmaze); TV only | none | Settings tab, Advanced ([Configuration](configuration.md#all-settings)) |
| `local` | `library_sync` only | none | none |

The `local` driver is the library without an account: the library, the watch history and the ratings live only in
PenguWatch. `tvmaze` is an account-free TV catalogue and schedule (below). There is no account-free anime driver yet (see [Not shipped: AniList](#not-shipped-anilist)).

## simkl

[SIMKL](https://simkl.com) is the default driver for every role, and the only one that ships with an account. It offers
every capability (`drivers/simkl/`):

- `library_sync`: the account's library. You create your own free SIMKL app
  ([simkl.com/settings/developer](https://simkl.com/settings/developer/app/new)); its client ID (the "app id") identifies
  PenguWatch to SIMKL, and you connect with a device PIN (`connect_kind` is `device_code`; the steps are in
  [Getting started](getting-started.md#4-choose-where-your-library-lives)). The login renews itself where SIMKL issues a
  refresh token. How the delta sync, its API use and the login states work: [Sync](sync.md#simkl).
- `catalog`, `schedule`, `discovery`: search, details, episode lists and sequels, the airing schedule, trending titles
  and recommendations.
- `ratings`: SIMKL's and IMDb's (MyAnimeList's for anime), stored in the `simkl_rating` and `imdb_rating` columns.

Options: the Settings tab ([Configuration](configuration.md#all-settings)); the environment variables `SIMKL_CLIENT_ID` and
`SIMKL_ACCESS_TOKEN` override the client ID and the access token.

## The local library driver

The library set to Local (Settings > Data sources, `drivers.library: local`). Titles still come from the catalogue drivers (`catalog`), so adding one works as before;
what changes is that nothing is sent to or read from an account:

- nothing contacts any network for the library, and nothing is ever delivered. The only outbox rows written are the
  unapproved ones that keep your edits for the account you used before (see [Switching](#switching-the-library-driver));
  with no earlier account, none;
- there is no connect step; the status reads connected and ok, "Local only";
- the worker does no pulls (it still refreshes titles' details, episode lists and the schedule from the catalogue and
  schedule drivers);
- review mode does not apply and is hidden.

### The `remote` flag

`LibrarySync.remote` (drivers/base.py) says whether an account sits behind the library. It is an explicit attribute, not
something the engine infers (a `key_provider` that happens never to match would work by accident). When `False`, the
sync engine: delivers nothing (`flush_outbox`), never calls `plan_pull`, `pull` or `push`, reports review mode as off,
and a run of the worker only does the catalogue upkeep. It writes no outbox row for the library itself; `_queue` writes
only the unapproved rows for the last account driver described under Switching. A driver with an account
sets `remote = True`. The driver's class carries the same flag (and `key_provider`) so the start-up code can read it
before the driver is built.

Titles added while the library is local are found again through their catalogue ids (`title_ids`), so adding the same
title twice from a search finds it. They also get a `local` id (`drivers.base.LOCAL_ID`), which marks a title that was
never on an account.

### Switching the library driver

Switching deletes no title, episode, watch or rating. What is kept, and what you decide:

- **An account driver to `local`:** every title, episode, watch and rating stays. Changes queued for the account driver
  and not yet delivered stay in the outbox, neither delivered nor deleted. Each time a library driver with an account
  starts, the engine records it (`library.last_account` in app_state). While the library is local, a change you make
  to a title that has that driver's id (episodes watched or unwatched, status, rating, removing the title or deleting
  it with "also remove from the library") is also written to the outbox for it, always **unapproved**, whatever the
  review-mode setting, and never delivered while local. Nothing is written for a title without that id, for a title
  added while local (it carries a `local` id: it was never on the account), or when no account driver ever ran
  (the record is written when the app starts with the account driver, so a database that has only ever run `local`
  has none). The Services & Sync tab and `GET /api/drivers` (`inactive_outbox`, `waiting_for_inactive`) count all
  these rows as waiting for an inactive driver.
- **`local` back to the account driver:** the rows queued before are delivered as usual, and the unapproved rows kept
  while local wait for you in *Review pending changes* (Services & Sync, and the Sync Now dialog), **also with review
  mode off**. Until you decide, a pull treats such a title like any title with an unsent change: it does not overwrite
  its status, progress or rating, and a title you deleted while local is not imported again (nor staged for import).
  *Approve* sends the row; *Decline* drops it and takes the account's version for that title (a deleted title comes
  back). Edits made while local are therefore only lost if you decline them. Decline is refused (409; the button is
  disabled with the reason) while the library is still local or the account is not connected: there is no account to
  take the version from. A newer change to a title replaces the older unapproved rows it contradicts, so approving
  old rows later cannot undo it ([Sync](sync.md#how-the-engine-works)).
- A pull's "missing from the listing" rule removes only titles that have the account driver's id and a `local` id
  absent, so titles added while local (no account id, or one from the catalogue plus a `local` id) are never removed by
  it. They are not pushed to the account either.
- A title added from the catalogue while the account driver is the library driver but not yet connected is queued as
  "add if missing": held until a full pull has shown what the account has. A title the account has takes the account's
  state; one it lacks is sent and kept ([Sync](sync.md#how-the-engine-works)).

## What the page reads

The page names no service (`tests/test_web_driver_neutral.py`). `GET /api/drivers` describes every driver: `label`,
`offers`, `running`, `key_provider`, `remote` (library drivers), `connect_kind` (how the account connects; `null`:
nothing to connect), `extras` (`credentials`: an app id and token form; `diagnose`), `title_urls` (a title's page on the
service, per media type, `{id}` for its id), `home_url` (credited on the Services & Sync tab) and `rating_sources`. A
driver declares them as class attributes next to `name` and `label` (`Driver` in drivers/base.py); all are optional.

## tvmaze

[TVmaze](https://www.tvmaze.com/api) is a public TV database. No account, key or sign-up.

**Offers**, for TV shows only (TVmaze has no movies; it marks anime with the Anime genre, and such shows are typed
`anime`):

- `catalog`: search, a show's details by TVmaze id (network or web channel, genres, overview, poster, runtime, status,
  other names), the episode list (numbered episodes only; TVmaze's unnumbered specials are left out), and `resolve`
  from an IMDb or TheTVDB id. It reports `tvmaze`, `imdb` and `tvdb` ids, so the catalogue chain can match titles
  between catalogues. It has no sequels (`related` is empty).
- `schedule`: the next episode of each running TV title in the library that has a TVmaze id (one request per title,
  at most `schedule_max_titles` per run).
- `ratings`: TVmaze's average (0 to 10). It is a built-in, read-only source with no column in `shows`: the value is
  kept in `title_ratings` with the link to the show's TVmaze page, and appears with the other sources (`sort_by=rating:tvmaze`,
  `min_rating=tvmaze:7`). It is filled when a title is added and when its details are refreshed. If you already have
  a custom rating source with the id `tvmaze`, it is left untouched (its values too): the built-in rating
  stays off, a warning is logged and shown on the System tab; rename or delete your source and restart to turn it on.
- Not offered: `library_sync` (no accounts at TVmaze), `discovery` (no trending or recommendations), movies.

**Use it:** on the Settings tab, Data sources: Catalogue `tvmaze` then `simkl`, Schedule `tvmaze` (`drivers.catalog: [tvmaze, simkl]`, `drivers.schedule: [tvmaze]`; applies after a restart). With `library: simkl` and no SIMKL login,
titles are added and followed from TVmaze alone. The first catalogue that has the title's own id answers.

**Limits and manners:** TVmaze allows at least 20 calls per 10 seconds per IP address and answers 429 beyond it. The
driver keeps to 18 per 10 seconds (`rate_limit_calls`), retries a 429 or 5xx twice after the pause TVmaze asks for
(`Retry-After`, at most 30 s) or a growing back-off, sends `User-Agent: PenguWatch/<version> (self-hosted; +https://www.tvmaze.com/api)`,
holds one connection at most two (TVmaze blocks addresses with several idle connections), and reports each request to
the upstream metrics as service `tvmaze`. `penguwatch doctor` checks api.tvmaze.com is reachable.

**Terms** (https://www.tvmaze.com/api): the data is licensed CC BY-SA 4.0. TVmaze must be credited as the source, by a
link back to TVmaze; the page names no exact wording. Anything that displays TVmaze data shows the driver's
`attribution`: "TV data from TVmaze", linked to https://www.tvmaze.com (the API lists it per driver in
`GET /api/drivers`). The ShareAlike condition applies to adapted TVmaze data you redistribute (an export you publish
carries the same licence); using it in your own library does not publish it. Responses are cached by TVmaze for up to an
hour. TVmaze asks for a unique User-Agent (sent), and offers a paid Enterprise API for other needs.

## Not shipped: AniList

An AniList driver (anime catalogue, airing schedule, trending, averageScore) was designed and not built. AniList's
[terms of use](https://docs.anilist.co/guide/terms-of-use) prohibit using the API "within competing, non-complementary
services of the same nature", naming anime and manga list or tracker services, for user data and media data alike.
PenguWatch tracks what you watch, including anime, so it is such a service, whether or not it also adds a
library of its own. Terms also forbid using the API as a backup or data store and "hoarding" data (PenguWatch keeps a
copy of every title's details), and ask that an app using "AniList" in its name say it is unofficial. Rate limit: 90
requests a minute (30 while the API is degraded). Until AniList permits such use there is no AniList driver; the anime in TVmaze (genre Anime) is the account-free
option, and an anime driver for another service is welcome.

## Writing a driver

A driver is a package in `src/penguwatch/drivers/<name>/` that turns one service's answers into the neutral types of
`drivers/base.py` and is listed in the registry. Nothing else changes: the page, the API and the sync engine learn
what a driver offers from its class. Read the TVmaze driver (`drivers/tvmaze/`) as the model of a small one and
`tests/test_driver_tvmaze.py` as the model of its tests. Drivers ship with PenguWatch (no plugin loading), so a new one
is a pull request and its dependencies go through the dependency gate like any other.

### Check the service's terms first

Before writing a line, read the service's terms of use and API terms. Look for:

- **Who may use the API, and for what.** A service may forbid tracker or "competing" apps, backups, or storing its data
  (that is why [there is no AniList driver](#not-shipped-anilist)). A tracker that keeps a copy of every title's details
  has to be allowed to.
- **Attribution.** If the licence asks for a credit (TVmaze's CC BY-SA does), set the driver's `attribution`; the page
  then shows it wherever that data appears (below).
- **Rate limits and identification.** Keep under the limit with a `RateLimiter`, and send a `User-Agent` that names
  PenguWatch and its version (`TvmazeClient.user_agent`).
- **Keys.** A service that needs a key or login per user is a library driver with a connect flow; one that needs a key
  per installation takes it as an option (`options_model`, `env_options`).
- **Whether a self-hosted app may redistribute the data.** The data stays in the user's own database; an export carries
  whatever licence the data has.

If the terms do not allow it, do not write the driver. Put what you found in the driver's docstring and in this page.

### Package layout

```
src/penguwatch/drivers/<name>/
    __init__.py   the Driver class: attributes and build(). Light: the registry imports it at start-up
    options.py    the pydantic model of config.yaml's `<name>:` section, when there are options
    client.py     the HTTP client: base URL, User-Agent, rate limiter, retries, errors
    mapper.py     pure functions: the service's JSON -> Title, Episode, ScheduledEpisode ...
    service.py    the running driver: one object per capability, and aclose()
tests/data/drivers/<name>/   recorded answers
tests/test_driver_<name>.py
```

`__init__.py` must stay light: import `client`, `mapper` and `service` inside `build()` (as `TvmazeDriver.build` does),
so listing the drivers does not load every HTTP client. `drivers/local/__init__.py` is the smallest complete driver
(one file, a library with no account).

### The Driver class

A class with these attributes (the `Driver` protocol in `drivers/base.py`; `tests/test_driver_layer.py` checks
that `name`, `label`, `offers`, `secret_state_keys` and `rating_sources` exist on every registered driver). All are class attributes, so the start-up code can read them before the
driver is built.

| Attribute | Needed | Meaning |
|---|---|---|
| `name` | yes | The registry key and the name used in config.yaml's `drivers:` and as the name of the driver's own `<name>:` section. Lower case. Also the default `key_provider`. |
| `label` | yes | What the page calls the service. |
| `offers` | yes | `frozenset` of `Capability` values the driver offers (`CATALOG`, `LIBRARY_SYNC`, `SCHEDULE`, `DISCOVERY`, `RATINGS`). `check_role` refuses a role the driver lacks the capability for. |
| `options_model` | yes | A pydantic model of the driver's config.yaml section, or `None` when it has no options. Every field needs a default: a missing section is parsed as `{}`. Bound every number (`ge=`, `le=`), as `TvmazeOptions` does. |
| `rating_sources` | yes | A tuple of `BuiltinRatingSource` the driver fills; `()` for none ([below](#built-in-rating-sources)). |
| `secret_state_keys` | yes | The `app_state` keys that hold credentials, without the namespace; `()` for none. They are left out of backups and exports (`api/exports.py` builds the full keys with `state_key(name, key)`). Store credentials only under `state_key(name, key)` (`driver.<name>.<key>`). |
| `env_options` | no | Tuple of `(environment variable, option name)` pairs: a variable that is set overrides that field of the options (SIMKL's `SIMKL_CLIENT_ID`). Read by `config.py`. |
| `settings` | no | Tuple of `DriverSetting(field, section, label, effect, unit=None, live=True)`: the options of `options_model` the Settings tab shows (keyed `<driver>.<field>`, grouped under the driver's label; `section` is `sync` or `advanced`). Type, range and default come from the field, so the range lives in one place. `live=False` marks an option read once when the driver is built ("applies after a restart"). A test fails for a field of the model that is neither here nor in `not_on_page`. |
| `not_on_page` | no | `{field: reason}` for options the tab must not show (SIMKL's `client_id` and `access_token`: credentials). |
| `legacy_defaults` | no | The defaults the option had before the Settings tab, for the one-time import (only where they differ from the model's). |
| `health_url` | no | A URL `penguwatch doctor` fetches to say whether the service is reachable. |
| `attribution` | no | `Attribution(text=..., url=...)`: the credit the service's terms ask for. `GET /api/drivers` lists it and the page shows it ([below](#showing-a-credit)). |
| `remote` | library drivers | `True` when an account sits behind the library, `False` when it lives only in PenguWatch ([The `remote` flag](#the-remote-flag)). |
| `key_provider` | no | The id name the driver's titles are known by in `Ids` (default: `name`). The local driver's is `LOCAL_ID`. |
| `connect_kind` | no | How an account connects (`"device_code"`); absent when there is nothing to connect. The page renders the connect flow generically from the `ConnectStep`s. |
| `extras` | no | Extra controls: `"credentials"` (an app id and token form; needs `set_credentials` on the library) and `"diagnose"` (a connection check; needs `diagnose` on the library). |
| `title_urls` | no | `{media type: URL template}` for a title's page on the service, `{id}` for its id; the page links to it from the details dialog when the title has the driver's id. |
| `home_url` | no | The service's home page, credited on the Services & Sync tab. |

### `build()`

```python
@classmethod
async def build(cls, repository, options, *, app_version: str, on_request=None):
```

`options` is the parsed `options_model` (or `None`), `repository` the database (`DatabaseRepository`; read and write the
driver's own state through `repository.get_state` / `set_state` under `state_key(name, key)`), `app_version` for the
User-Agent, `on_request` the telemetry hook of the HTTP client: call `on_request(service, path, http_status, seconds)`
after each request (status `0` for a network error) so the call shows in the upstream metrics, and never let it raise.

It returns the running driver: an object with `library`, `catalog`, `schedule` and `discovery` (each `None` for what
the driver does not offer; `offers` and these must agree) and `async aclose()`, which closes the HTTP client. It is built
once, at start-up, by `build_drivers` (`sync/active.py`), for every driver config.yaml names.

### The capabilities

Each capability is a small protocol in `drivers/base.py`; implement exactly the methods it lists. Unknown values stay
`None` or empty, never guessed. Return plain data: a `Title`, an `Episode`, never the service's JSON.

**`catalog` (`CatalogProvider`)**: set `key_provider` on the object, and `async ready() -> bool` (not in the protocol,
but the API calls it on every catalogue before using it: say `False` while a key or login is missing).

- `search(query, media_types, limit=10, detailed=0) -> List[Title]`: best first, only the media types asked for
  (`"show"`, `"anime"`, `"movie"`); `[]` for none. A search result needs the ids and enough details to show a card.
  `detailed` is accepted for catalogues whose search gives less than the details; ignore it when it does not apply.
- `title(media_type, ids) -> Optional[Title]`: the full details, looked up by the driver's own id (`ids[key_provider]`);
  `media_type` is `None` when the type is not known. `None` when the title is not found or is a type the driver lacks.
- `episodes(media_type, ids) -> Optional[List[Episode]]`: airing order, by season and number (season 0 is specials);
  `None` when it could not be fetched (so it is tried again later), `[]` when none are known.
- `related(media_type, ids) -> Optional[List[RelatedTitle]]`: direct sequels; `[]` when the service has no such thing.
- `resolve(media_type, ids) -> Optional[Tuple[Ids, str]]`: given another provider's ids (an IMDb or TheTVDB id), the
  ids including this driver's own and the media type; `None` when not found. Return `(dict(ids), media_type)` unchanged
  when the title already has the driver's id.

**`library_sync` (`LibrarySync`)**: the account's library. It is the largest interface; the SIMKL driver
(`drivers/simkl/service.py`, `SimklLibrary`) is the full example. The engine owns the outbox, review mode, back-off, the
cursor and applying changes, so the driver only:

- keeps its login (`is_connected`, `needs_reconnect`, `maintain`, `on_auth_error`, `confirmed`, `status`, `connect`,
  `disconnect`) and the pull intervals (`pull_interval_minutes`, `idle_pull_interval_minutes`, `idle_after_hours`);
- pulls: `plan_pull(cursor, full)` decides what to read since the stored `cursor` (any JSON value; `PullPlan(mode="none")`
  when nothing changed), `pull(plan, cursor)` yields `PullBatch`es of `SyncChange`s one part at a time, and
  `decode_entry(media_type, native)` turns a staged record back into a `LibraryEntry` when the user accepts it in review
  mode. A batch with `listing=True` and `remote_keys` says what the account holds for that media type, so a pull can
  remove titles that left it. The engine stores the new cursor only after everything was applied;
- pushes: `batch_key(op)` (ops of different titles with equal keys may share a request; `None` to send alone) and
  `push(ops)`, which delivers `SyncOp`s (`watched`, `unwatched`, `status`, `rate`, `unrate`, `remove`; the `OP_*` names)
  and raises `SyncError` when it fails;
- `entries(media_type)`: every title of one type in the account now.

A library driver whose `remote` is `False` has no account: its methods do nothing (`LocalLibrary`).

**`schedule` (`ScheduleProvider`)**: `upcoming() -> List[ScheduledEpisode]`, episodes airing soon. The engine keeps the
ones for titles in the library, matching by the first entry of `ids`, so put the driver's own id first. Do not make more
requests than the service tolerates: TVmaze's schedule asks about at most `schedule_max_titles` titles per run and stops
after three failures.

**`discovery` (`DiscoveryProvider`)**: `key_provider` (the id a seed title needs), `ready()`,
`trending(media_type, interval)` (`interval`: `today`, `week`, `month`) and `recommendations(media_type, ids)` returning
`(Title, score)` pairs, best first. `Title.extra` may carry `"rank"` and `"watchers"`.

**`ratings`**: not an object but `rating_sources` on the class, with the values on `Title.ratings`
([below](#built-in-rating-sources)).

### Errors

Raise `SyncError(kind, message, permanent=False)` (`drivers/base.py`) from a library driver's `plan_pull`, `pull` and
`push`; the engine acts on the `kind`:

| `kind` | Meaning | What the engine does |
|---|---|---|
| `auth` | The login no longer works. | Stops the run, calls `on_auth_error`, shows the reconnect banner. |
| `scope` | The login may read but not write. | Stops the run and shows `message`. |
| `duplicate` | The service already has it. | The op counts as done: the outbox row is deleted. |
| `transient` | Try later: rate limit, server error, network. | Backs the row off and retries; a batch is backed off together. |
| `rejected` | The service refused it. `permanent=True` when retrying cannot help. | Backs off and retries; a permanent refusal is dropped after three attempts. |
| `unbatchable` | (`push` only) the ops cannot share a request. | The engine sends them one by one. |

A catalogue or schedule method may raise any exception: the engine logs it and treats the title as not found, or the
schedule as empty, for that run. Raise your own error type (`TvmazeError`) from the client and let `get(..., missing_ok=True)`
turn a 404 into `None` where a missing title is normal. Never put a token, a key or a password in an error message or a
log line.

### Ids

A title is a set of ids, one per provider: `Ids = {"tvmaze": 44933, "imdb": "tt11280740", "tvdb": 371980}`.

- Build ids through `normal_ids()` (`drivers/base.py`): provider names in lower case, numbers as `int` (also when they
  arrive as digit strings), other values trimmed, and entries with no usable value (`None`, `""`, `0`, a bool) left out.
- Report every id the service gives (`imdb`, `tmdb`, `tvdb`, its own). The catalogue chain matches titles across
  catalogues by them, and a driver that reports a wrong id attaches the wrong title; report only ids the service states.
- `key_provider` names the driver's own id. A catalogue is asked about a title only when the title has that id
  (`catalog_for` in `sync/enrich.py`); `resolve` is how a title known only by an IMDb id gets one.
- The ids of a title are not unique per provider (anime seasons share them); never assume one id means one title.

### The rate limiter

`drivers/ratelimit.py` has `RateLimiter(calls, per, *, clock=time.monotonic, sleep=asyncio.sleep)`: at most `calls`
requests in any `per` seconds, served in order. Call `await limiter.wait()` before every request. Use a limit a little
under the service's published one and make it an option, as `TvmazeClient` does (`rate_limit_calls`,
`rate_window_seconds`). `clock` and `sleep` are parameters so tests run it with a fake clock (`Clock` in
`tests/test_driver_tvmaze.py`). Retry a 429 or 5xx only a few times, after the pause the service asks for (`Retry-After`),
with an upper bound.

### Built-in rating sources

A driver that reports a rating declares a `BuiltinRatingSource(id, name, label, column=None, votes_column=None,
scale_min=0, scale_max=10, decimals=1, show_on_cards=True)` in `rating_sources` and puts the value on the title:
`Title.ratings = {"<id>": RatingReading(value, votes=None, fallback=False)}`. A `fallback` reading only fills a title
that has no value yet.

- **With a column** (SIMKL's `simkl_rating` and `imdb_rating`): `column` names a column of `shows` that holds the value.
  The columns exist from the first migrations and are storage only; a new driver should not add one.
- **Without a column** (`column=None`, TVmaze): the value goes to `title_ratings`, like a custom rating source, but the
  source is built in: listed read-only in `/api/rating-sources` and filled by its driver, never imported or edited.
  `store_ratings` (`sync/enrich.py`) writes it whenever the title's details are fetched. Put the link to the title's page
  on the service in `Title.extra["url"]` (`sync/titles.py` stores it with the value). The source exists only while a
  config names the driver (`stored_builtin_specs`), and its id may not be taken by a custom source (`RESERVED_IDS`).

### Showing a credit

Set `attribution` when the terms ask for a credit. The page reads it from `GET /api/drivers` (it never names a service:
the text and link come from the API) and shows it for every running driver that has one: in a line at the foot of every
tab, on the Services & Sync tab, and in a title's details when the title has the driver's id. Nothing is shown when no
running driver has one. Do not put the wording in the page.

### Registering the driver

Import the class in `drivers/__init__.py` and add it to `DRIVERS`:

```python
from penguwatch.drivers.example import ExampleDriver
DRIVERS: Dict[str, type] = {driver.name: driver for driver in (SimklDriver, LocalDriver, TvmazeDriver, ExampleDriver)}
```

The registry is the one place outside a driver's package that names it. Everything else is derived from it: the
config validation, the `GET /api/drivers` listing, the rating sources, the backup filter (`secret_state_keys`) and
`penguwatch doctor` (`health_url`).

### Config validation

`config.yaml`'s `drivers:` (`DriversConfig` in `config.py`) is checked when the config loads, through `check_role`
(`drivers/__init__.py`): a name that is not in `DRIVERS`, or a driver that lacks the role's capability, stops the start
with a message naming the key and the driver (`drivers.catalog: the local driver does not offer catalog`). A driver's own
section (`example:` at the top level of config.yaml) is parsed by its `options_model` (`AppConfig._parse_driver_options`),
so a wrong value fails with the field's name. Declare each option the user may change in `settings` (see the table above;
it appears on the Settings tab without any front-end work), document it in [Configuration](configuration.md#all-settings)
and, if it matters for a first start, add a commented example to `config.example.yaml` (the copy under `src/penguwatch/`
must stay identical; a test checks that). After the first start the values on the Settings tab, not `config.yaml`, are
the ones in effect.

### The vendor-name guard tests

The service's name appears only in its own package. Two tests keep it so:

- `tests/test_driver_layer.py` scans every Python module under `src/penguwatch/` outside the allowed places
  (`VENDOR_PLACES`: the driver's package, the registry, migrations) for the vendor's name (`VENDOR`, now `simkl`) and
  for imports of its package. Add a guard for your driver's name the same way (a second `VENDOR`/`VENDOR_PLACES`
  entry, or a parametrized test); a driver that names another service in its own code is fine.
- `tests/test_web_driver_neutral.py` refuses the name of any registered driver, and of the services in `KNOWN`, anywhere
  under `web/` (HTML, JS, CSS). Add the new service to `KNOWN` if it is planned before it is registered. A page that needs
  something about a driver gets it from `/api/drivers` (a class attribute), not from a name in the page.

### Testing with recorded responses

Tests never reach the network. Record the service's real answers once (trim them to what the tests read; mark a
hand-written one as such), put them in `tests/data/drivers/<name>/`, and replay them with respx:

```python
with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
    mock.get(f"{BASE_URL}/shows/44933").respond(json=load("show_44933.json"))
```

`assert_all_mocked=True` makes a request nobody mocked fail instead of reaching the service; an autouse fixture applies it
to every test (see `no_network` in `tests/test_driver_tvmaze.py`). Test, as that file does:

- the registry entry (`offers`, `health_url`, `attribution`, the rating source) and config validation;
- the mapper on its own, including a bare record (every optional field missing) and zeros that mean "none";
- each capability method against recorded answers, including the not-found and failure cases;
- the rate limiter and retries with a fake clock (no real sleeping), and the User-Agent;
- the chain end to end: a title added through the API, its details, episode list and ratings stored;
- for a library driver, the push payloads against recorded requests and the pull against recorded listings, with the
  mutation check the data-critical code gets (break the code, see the test fail).

`scripts/test.sh` runs the suite in a container with no network; it must pass, and `scripts/test.sh --seed` runs the
characterization tests on a copy of a real library.

### A minimal worked example: a catalogue-only driver

A driver for an imaginary public movie database, "Filmbase" (`https://api.filmbase.example`, no key, CC BY 4.0, 10
requests a second). It offers only `catalog`. This is text for the guide, not shipped code.

`drivers/filmbase/options.py`:

```python
from pydantic import BaseModel, Field

class FilmbaseOptions(BaseModel):
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
```

`drivers/filmbase/__init__.py`:

```python
from typing import ClassVar, FrozenSet, Tuple

from penguwatch.drivers.base import Attribution, BuiltinRatingSource, Capability
from penguwatch.drivers.filmbase.options import FilmbaseOptions


class FilmbaseDriver:
    name: ClassVar[str] = "filmbase"
    label: ClassVar[str] = "Filmbase"
    offers: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CATALOG})
    options_model: ClassVar[type] = FilmbaseOptions
    rating_sources: ClassVar[Tuple[BuiltinRatingSource, ...]] = ()
    secret_state_keys: ClassVar[Tuple[str, ...]] = ()
    health_url: ClassVar[str] = "https://api.filmbase.example/"
    attribution: ClassVar[Attribution] = Attribution(text="Film data from Filmbase", url="https://filmbase.example")

    @classmethod
    async def build(cls, repository, options, *, app_version: str, on_request=None):
        from penguwatch.drivers.filmbase.service import FilmbaseService
        return FilmbaseService.create(options, app_version=app_version, on_request=on_request)
```

`drivers/filmbase/service.py`, with `client` a small wrapper like `TvmazeClient` (a rate limiter, a User-Agent, `get`)
and `mapper.film_title` turning one JSON record into a `Title`:

```python
from penguwatch.drivers.base import Title, normal_ids
from penguwatch.drivers.filmbase import mapper
from penguwatch.drivers.filmbase.client import FilmbaseClient

NAME = "filmbase"


class FilmbaseCatalog:
    key_provider = NAME

    def __init__(self, client: FilmbaseClient):
        self.client = client

    async def ready(self) -> bool:
        return True                               # no key to wait for

    async def search(self, query, media_types, limit=10, detailed=0):
        if "movie" not in media_types:
            return []
        found = await self.client.get("/search", {"q": query})
        return [t for t in (mapper.film_title(r) for r in found)][:limit]

    async def title(self, media_type, ids):
        fid = ids.get(NAME)
        if not fid or media_type in ("show", "anime"):
            return None
        record = await self.client.get(f"/films/{int(fid)}", missing_ok=True)
        return mapper.film_title(record) if record else None

    async def episodes(self, media_type, ids):
        return []                                  # a film has none

    async def related(self, media_type, ids):
        return []

    async def resolve(self, media_type, ids):
        if media_type != "movie":
            return None
        if ids.get(NAME):
            return dict(ids), media_type
        if ids.get("imdb"):
            record = await self.client.get("/lookup", {"imdb": ids["imdb"]}, missing_ok=True)
            title = mapper.film_title(record) if record else None
            if title:
                return {**ids, **title.ids}, "movie"
        return None


class FilmbaseService:
    name, label = NAME, "Filmbase"

    def __init__(self, client: FilmbaseClient):
        self.client = client
        self.library = self.schedule = self.discovery = None
        self.catalog = FilmbaseCatalog(client)

    @classmethod
    def create(cls, options, *, app_version, on_request=None):
        client = FilmbaseClient(app_version=app_version, timeout_seconds=options.timeout_seconds)
        client.on_request = on_request
        return cls(client)

    async def aclose(self):
        await self.client.aclose()
```

`mapper.film_title` returns `Title(media_type="movie", title=..., ids=normal_ids({"filmbase": r["id"], "imdb": r.get("imdb")}),
year=..., poster=..., overview=..., runtime=..., extra={"url": ...})`.

Then: add `FilmbaseDriver` to `DRIVERS`; `catalog: [filmbase, simkl]` in config.yaml uses it first, with `simkl` as the
fallback for what it does not know. The page shows "Film data from Filmbase", linked, in the footer, on Services & Sync
and in the details of a title that has a `filmbase` id, with no change to the page. Add `"filmbase"` to `KNOWN` in
`tests/test_web_driver_neutral.py` while it is only planned, record answers into `tests/data/drivers/filmbase/`, and
write `tests/test_driver_filmbase.py` after `tests/test_driver_tvmaze.py`.

### Checklist

- [ ] The service's terms allow a self-hosted tracker; the credit, if any, is `attribution`.
- [ ] Attributes complete; `offers` matches the capability objects `build()` returns.
- [ ] Ids through `normal_ids`; every id the service states is reported, none guessed.
- [ ] Every request goes through a `RateLimiter`; a User-Agent names PenguWatch and its version; `on_request` is called.
- [ ] No secret in a log or an error; credentials under `state_key()` and listed in `secret_state_keys`.
- [ ] Registered in `DRIVERS`; options documented in [Configuration](configuration.md) and `config.example.yaml`.
- [ ] Tests with recorded answers pass with the network off; the guard tests pass.
- [ ] This page's table of shipped drivers, the changelog and the README mention it.
