# API

PenguWatch is a REST API with a page on top of it. The same JSON routes the page uses are open to your scripts, and the
parts listed under [Stable endpoints](#stable-endpoints) are a promise: they will not change under you. The live
reference is **`/openapi.json`** on your PenguWatch address (import it into any OpenAPI viewer, Postman or a code
generator), with every route tagged and summarised. There is no built-in `/docs` page: the page policy forbids the
scripts Swagger UI needs.

## Authentication

Without an access token the API is open to anyone who can reach the port. With `PENGUWATCH_ACCESS_TOKEN` set (see
[Security](security.md)), send it as a bearer token on every request except the few open ones (below):

```bash
curl -H "Authorization: Bearer $PENGUWATCH_TOKEN" http://localhost:8080/api/shows?view=card
```

Open without the token: `/api/health`, `/login`, `/manifest.webmanifest`, `/sw.js`, `/favicon.ico`, the files under
`/static/`, and `/api/calendar.ics` when it carries its `?key=`. A missing or wrong token is `401` with `{"detail": "Authentication required."}`. Browsers use a cookie instead; scripts
never need one. A request that carries an `Origin` header (a browser) must send the app's own origin for anything that
changes data, else `403`; scripts that send no `Origin` are checked by the token alone. The `Host` header must be one
the app accepts (`421` otherwise; `PENGUWATCH_ALLOWED_HOSTS` adds names).

## Conventions

* JSON in and out (`Content-Type: application/json`); times are ISO 8601 in UTC. `id` is the PenguWatch id of a
  title (an integer); `ids` are its ids elsewhere, one per provider (here `simkl`, the default driver's): `{"simkl": 1234567, "imdb": "tt0113277",
  "tmdb": 949, "tvdb": 70322}` (numbers as integers, IMDb's as text; a provider without an id is left out).
* **Errors** have the shape `{"detail": ...}`. `detail` is a sentence, or for `422` a list of
  `{"type", "loc", "msg"}` telling which field is wrong (`loc` is `["body", "items", 3, "value"]` for the fourth item
  of an import). `404` unknown id, `409` a conflict (a name taken, a built-in or config-defined thing you tried to
  change), `413` body too large, `429` too many requests (with `Retry-After`).
* **Limits:** request bodies up to 256 KiB (`PENGUWATCH_MAX_BODY_BYTES`); collections are bounded (a rating import
  takes at most 1000 items). Rate limits are per client address and per minute: the rating import 60, a webhook test
  6, the backup download 3, a driver's sync or connection check 6, catalogue search 30; the other routes have their own limits or none. A `429` is safe to retry after the pause it names.
* Unknown query parameters and extra body fields are ignored; new optional fields may appear in answers, so read the
  ones you need and ignore the rest.

## Stable endpoints

These routes keep their path, parameters and answer fields. Within a major version they are only extended: a new
optional parameter, a new field in an answer, a new value of an open-ended string. They carry `x-stable: true` in
`/openapi.json`, and `tests/test_openapi.py` fails if one disappears.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/health` | Liveness: `{"status": "ok"}`. Open without the token, like the few others listed under Authentication. |
| `GET` | `/api/shows` | List titles. Filters `status`, `priority`, `household_tag`, `media_type`, `service_id`, `search`; `sort_by`; `min_rating`; `view=card` for the short shape. |
| `POST` | `/api/shows` | Add a title: give its `ids` (from a catalogue search), or a `title` to look it up by name. |
| `GET` | `/api/shows/{show_id}` | One title, in the full shape. |
| `PATCH` | `/api/shows/{show_id}` | Change status, priority, household tag, service, notes, your rating (`user_rating`, `clear_user_rating`) or progress (`watched_episodes`). |
| `DELETE` | `/api/shows/{show_id}` | Remove a title (`remove_from_library=false` keeps it in the library driver's account). |
| `POST` | `/api/shows/{show_id}/increment` | Mark the next episode watched. |
| `POST` | `/api/shows/{show_id}/progress` | Set progress by count, by episodes or by seasons. |
| `GET` | `/api/shows/{show_id}/episodes` | The episode checklist by season. |
| `POST` | `/api/shows/{show_id}/episodes/toggle` | Mark one episode watched or unwatched. |
| `GET` | `/api/shows/{show_id}/ratings` | Every rating source on a title, built-in and custom. |
| `PUT` | `/api/shows/{show_id}/ratings/{source}` | Set one custom value on a title. |
| `GET` | `/api/rating-sources` | List rating sources. |
| `POST` | `/api/rating-sources` | Create a rating source. |
| `PATCH` | `/api/rating-sources/{source}` | Edit a rating source. |
| `DELETE` | `/api/rating-sources/{source}` | Delete a rating source and its values. |
| `PUT` | `/api/ratings/{source}` | Import up to 1000 ratings at once. |
| `DELETE` | `/api/ratings/{source}` | Remove every value of a source (the source stays). |
| `DELETE` | `/api/ratings/{source}/{show_id}` | Remove one title's value. |
| `GET` | `/api/services` | The streaming services. |
| `GET` | `/api/webhooks` | The configured webhooks and their delivery state (never their address or secret). |
| `GET` | `/api/webhooks/events` | The events a webhook can subscribe to. |
| `POST` | `/api/webhooks/{name}/test` | Send a test event to one webhook. |
| `GET` | `/api/export/library.csv` | The library as CSV, with a column per custom rating source. |
| `GET` | `/api/export/backup.db` | A consistent copy of the database without the account login. |
| `GET` | `/api/calendar.ics` | The iCal feed (also reachable with `?key=`, see [Security](security.md)). |

### Titles

`GET /api/shows` returns a list of titles. The fields you will use most:

```json
{
  "id": 42, "ids": {"simkl": 1234567, "imdb": "tt0113277", "tmdb": 949},
  "media_type": "movie", "title": "Heat", "en_title": null, "year": 1995,
  "status": "completed", "priority": "medium", "household_tag": "both", "service_id": "netflix",
  "simkl_rating": 8.0, "imdb_rating": 8.3, "imdb_votes": 640000, "user_rating": 9.0,
  "ratings": {"letterboxd": {"value": 4.5, "votes": 1200, "url": "https://example.org/heat", "updated_at": "2026-11-01T10:00:00+00:00"}},
  "total_episodes": 1, "watched_episodes": 1, "aired_episodes": 1,
  "episodes_left": 0, "is_caught_up": true, "is_ready_to_binge": false,
  "genres": ["Crime", "Thriller"], "network": null, "notes": null,
  "created_at": "2026-01-02T10:00:00+00:00", "updated_at": "2026-11-01T10:00:00+00:00"
}
```

* `status` is `watching`, `plantowatch`, `hold`, `completed` or `dropped`; `media_type` is `show`, `anime` or `movie`.
* `simkl_rating` and `imdb_rating` are the built-in ratings the default (SIMKL) driver provides (the IMDb field holds the
  MyAnimeList score for anime). `user_rating` is **your** rating, which PenguWatch keeps in step with your library
  service's account. `ratings` holds the
  values of your own [rating sources](#rating-sources), by source id.
* `view=card` returns only the fields the library cards use, and in `ratings` only the sources marked "show on
  cards".

**Sorting:** `sort_by` is one of `priority` (default), `imdb`, `simkl`, `my_rating`, `title`, `next_air`, `recent`,
`interest` (adds `interest_score` and `interest_reasons`), or `rating:<source id>` to sort by a rating source, highest
first, titles without a value last (`rating:simkl`, `rating:imdb` and any custom source).

**Filtering by rating:** `min_rating=<source id>:<lowest value>`, repeated to combine, keeps titles rated at least
that: `/api/shows?min_rating=letterboxd:4&min_rating=imdb:7.5`. An unknown source is `422`.

### Rating sources

A rating source is a place your ratings come from: Letterboxd, a spreadsheet, a friend's scores. `SIMKL`, `IMDb` and `TVmaze`
are built in and read-only (each while its driver runs); you add your own on the Ratings tab (see
[Configuration](configuration.md#services-and-ratings)) or with `POST /api/rating-sources`:

```json
{"id": "letterboxd", "name": "Letterboxd", "label": "LB", "scale_min": 0, "scale_max": 5, "decimals": 1, "show_on_cards": true}
```

`id` is a lowercase slug of 1 to 32 characters (`a-z`, `0-9`, `-`, `_`); the built-in ids (`simkl`, `imdb`, `tvmaze`), `mal`, `user`, `my` and
`your` are taken. `label` is up to 8 characters, for badges. `decimals` (0 to 3) is how many decimals to show. A source
imported from a 0.15 `config.yaml` is an ordinary source after the first start, so `"read_only"` is true only for the built-ins.
A scale cannot be narrowed below values that are already stored (`409`).

`GET /api/rating-sources` lists them, built-ins first, with `rated_titles` (how many titles have a value).

**Bulk import:** `PUT /api/ratings/{source}`

```json
{"items": [
  {"match": {"imdb": "tt0113277"}, "value": 4.5},
  {"match": {"title": "Alien", "year": 1979}, "value": 5, "votes": 3100, "url": "https://example.org/alien"},
  {"match": {"tmdb": 348, "media_type": "movie"}, "value": 4}
]}
```

`match` is exactly one external id under its provider's name, as in a title's `ids` (`imdb` (`tt1234567`), `tmdb`,
`tvdb`, `simkl`, ...), or `title` with an optional `year`; `media_type` (`show`, `anime`, `movie`) narrows any of them
(TMDB numbers are shared by a film and a series). Names are compared ignoring case, accents and punctuation, against
the title, the English title and the catalogue's alternative titles.
`votes` and `url` (an `http(s)` address) are optional and replace what was stored. Sending an item again replaces its
value. The answer:

```json
{"updated": 2, "unchanged": 0,
 "unmatched": [{"index": 1, "match": {"title": "Alien Resurrection", "year": 1997}}],
 "ambiguous": [{"index": 2, "match": {"tmdb": 348}, "candidates": [{"id": 7, "title": "Alien", "year": 1979, "media_type": "movie"}, {"id": 9, "title": "Alien", "year": 2025, "media_type": "show"}]}]}
```

`index` is the item's position in your request. Unmatched and ambiguous items are skipped, not errors. A value outside
the source's scale, or a malformed item, refuses the **whole batch** with `422` and writes nothing. More than 1000
items is `422`; a body over the size limit is `413`.

### Webhooks

`/api/webhooks` reports on your webhooks and their delivery state; it never returns an address or secret. They are
created and changed with the Settings routes below, not here. See [Extending](extending.md#event-webhooks) for the
events and payloads.

### Settings

The Settings tab is a page over these routes. They are **not stable** (they may change in any release), they need the
access token like every `/api` route, and they are in `/openapi.json` under the tag "Settings". Errors have the shape
`{"detail": "...", "errors": [{"where": "values.notify.hour", "message": "Enter at most 23"}]}`; `where` is
`values.<key>`, `lists.webhooks[<i>].<field>` or `secrets.<slot>`, and `message` never repeats what you sent.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/settings/schema` | Every setting with its section, group, label, help text, unit, type, range, choices, default, whether it applies live and which environment variable pins it; the webhook list; the secret slots; the drivers and roles; the themes. Static per build; carries an `ETag` and answers `304` to `If-None-Match`. |
| `GET` | `/api/settings` | The state: `revision`, `values` (per key: `value` in effect, `stored`, `source` of `default`, `stored` or `env`, `pending_restart`, `live_failed`, and `env_ignored`: why a variable that would pin it is ignored, naming the variable, never its value), `lists.webhooks` (items without addresses, and each one's state), `secrets` (per slot: `set`, `source`, and the `host` of an address), `about` (version, listen address, paths, whether a token is set, what the one-time import skipped) and `token_set`. |
| `GET` | `/api/settings/values` | Just `{"revision": N, "values": {key: effective value}}`, for scripts. |
| `POST` | `/api/settings/validate` | Checks a draft (same body as a save) without saving: `ok`, `errors`, `warnings`, a `summary`. 60 a minute. |
| `PUT` | `/api/settings` | Saves. 20 a minute. `?source=restore` (a version loaded with `GET /versions/{n}`) or `?source=yaml_import` (an Import draft) is recorded as the version's source; the default is `save`. |
| `GET` | `/api/settings/versions` | The last 50 saves, newest first: revision, time, source, summary. |
| `GET` | `/api/settings/versions/{n}` | One version as a draft you can put back with a save. |
| `GET` | `/api/settings/export` | The stored settings as `text/yaml` (an attachment `penguwatch-settings.yaml`), in `config.yaml` syntax, without any address, secret or slot. |
| `POST` | `/api/settings/import` | A YAML body (`text/yaml` or `application/x-yaml`, at most 64 KiB) becomes a draft `{values, lists, notes, kept}`. Applies nothing. 20 a minute. |

A save:

```json
PUT /api/settings
{"base_revision": 7,
 "values": {"optimizer.max_active_services": 2, "notify.hour": null},
 "lists": {"webhooks": [{"id": "home-assistant", "name": "Home Assistant", "events": ["title.added"],
                         "secret_env": null, "secret_file": null}]},
 "secrets": {"webhook.home-assistant.url": "https://192.0.2.10/hook", "webhook.home-assistant.secret": "0123456789abcdef"}}
```

* `base_revision` is the `revision` you read. Keys left out are untouched. `"key": null` resets a setting to its
  default; `"webhooks": null` resets the list (to none).
* `secrets` is write-only: a string sets a slot, `""` clears it, and no route ever returns one. The slots are
  `notify.url`, `webhook.<id>.url` and `webhook.<id>.secret`. A new webhook needs its address in the same save.
* A key or slot pinned by an environment variable, and a webhook secret while the webhook names a `secret_env` or
  `secret_file`, is `400`, naming the variable (or saying the webhook reads its secret from a file).
* A save can't set a new or changed `secret_env` or `secret_file` (an imported one can be kept or cleared, with `null`),
  can't give a new address to a webhook that still signs with one, and refuses an address with `user:password@` or one
  that is this machine or link-local (the digest address too), and a signing secret under 16 characters: each is a
  `422` with its `where` (`lists.webhooks[<i>].secret_env`, `secrets.webhook.<id>.url`, ...). Details in
  [Security](security.md#settings-and-secrets).
* The answer (`200`) is `{"revision": 8, "summary": "...", "applied": [...], "restart_needed": [...], "live_failed":
  [...], "reload": false}`: what changed (the summary never contains a secret or an address), which settings apply only
  after a restart, which live applier failed, and whether the page should reload (household names).
* `422` lists what was refused and nothing is saved. `409` (`{"detail": "Changed elsewhere since you opened the page",
  "revision": 9, "conflicts": [...]}`) means a key, the list or a slot that this save touches changed since your
  `base_revision`; read again and merge. `500` means `secrets.json` (or the database) could not be written and nothing
  was saved.

## MCP

`POST /mcp` is a read-only [MCP](mcp.md) endpoint for AI assistants, off by default (`mcp.enabled`). It is not part of the
REST API above and is not in `/openapi.json`, but the same guards apply: the bearer token, the `Host` check, the `Origin`
rule and the body cap.

## What may change

Everything not in the table above is for the page that ships with the app: the drivers (`/api/drivers`: the driver of each role, what each offers, `remote`, the changes waiting for an inactive driver; and the library
driver's status, connect steps, sync, disconnect and diagnostics under `/api/drivers/{driver}/...`), the catalogue
search (`/api/catalog/search`), the outbox and review, discover, the optimizer and planner, insights, history (`/api/history?days=7|30|365&include_bulk=false`: totals for the last `days` local days and the `days` before, series, top shows, hours and cost per hour by service, genres, weekday and hour-of-day counts; any other `days` is a 422), tidy, the System tab, the themes (`GET /api/themes`: the palettes the page can show and the server's default, see [Themes](themes.md)), and the page's helpers. Those routes may
change shape or disappear in any release, including a patch release. If you need something from them, ask for it to
become stable.

The shape of the **event webhook payloads** follows the same rule as the stable endpoints (`version` is in every
body). `/metrics` (Prometheus) keeps its metric names; new metrics may appear. (The driver layer renamed the SIMKL
gauges: `penguwatch_library_connected`, `penguwatch_library_last_sync_age_seconds` and `penguwatch_library_last_error`,
each with a `driver` label.)

## Versioning

The API is versioned with the app (`GET /openapi.json` has `info.version`). A breaking change to a stable endpoint is a
new major version of PenguWatch and is listed first in the [changelog](../CHANGELOG.md).
