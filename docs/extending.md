# Extending PenguWatch

PenguWatch is extended from the outside: your own scripts and services talk to it over the [API](api.md), it talks to
them with event webhooks, and data comes in by import. There is no plugin loading; a change that needs to live inside
the app is a component (see the end), and a new service (a tracker, a catalogue) is a [driver](drivers.md).

* [Bring your own ratings](#bring-your-own-ratings): push a score from any source and sort, filter and export by it.
* [Event webhooks](#event-webhooks): get a call when a title is added, a status changes, an episode is watched.
* [Add a driver](#add-a-driver): a new service (a tracker, a catalogue, a schedule, a ratings source).
* [Add a component](#add-a-component-to-the-app).

## Bring your own ratings

1. **Define a source**, once, on the Ratings tab (**New source**: name, badge label, id, decimals shown, lowest and
   highest value, and whether it shows on the cards), or with the API (`POST /api/rating-sources`, see [API](api.md#rating-sources)). Sources
   are not part of the Settings tab or of `config.yaml`: a `ratings: sources:` list in a 0.15 `config.yaml` is read once,
   at the first start of the version with the Settings tab, and from then on the sources are ordinary ones you can edit.
2. **Push values** with `PUT /api/ratings/letterboxd`, up to 1000 per request. The complete example below does it from a
   CSV file.
3. **Use them:** sort the library by the source (`sort_by=rating:letterboxd`), filter (`min_rating=letterboxd:4`),
   see its badge on the cards, its value on a title's details, and a `rating_letterboxd` column in the CSV export.

Custom ratings are for display, sorting, filtering and export. They do not change the interest score or the
optimizer, and they are never sent to your library account; your own rating of a title (`user_rating`) is the one that syncs.

### Push ratings from a script

[`docs/examples/push_ratings.py`](examples/push_ratings.py) is a complete script (it needs only `httpx`). It reads a
CSV with a `title`, `year` and `rating` column, creates the source if it is missing, sends the ratings in batches and
lists what it could not match:

```python
import csv
import os
import sys

import httpx

client = httpx.Client(
    base_url=os.environ.get("PENGUWATCH_URL", "http://localhost:8080"),
    headers={"Authorization": f"Bearer {os.environ['PENGUWATCH_TOKEN']}"},
    timeout=60,
)
source = "letterboxd"

if not any(s["id"] == source for s in client.get("/api/rating-sources").json()):
    client.post("/api/rating-sources", json={
        "id": source, "name": "Letterboxd", "label": "LB", "scale_min": 0, "scale_max": 5, "show_on_cards": True,
    }).raise_for_status()

with open(sys.argv[1], newline="", encoding="utf-8") as f:
    items = [
        {"match": {"title": row["title"], "year": int(row["year"])}, "value": float(row["rating"])}
        for row in csv.DictReader(f)
    ]

for start in range(0, len(items), 500):
    answer = client.put(f"/api/ratings/{source}", json={"items": items[start:start + 500]})
    answer.raise_for_status()
    result = answer.json()
    print(f"{result['updated']} updated, {result['unchanged']} unchanged")
    for miss in result["unmatched"] + result["ambiguous"]:
        print("  no single match for", items[start + miss["index"]]["match"])
```

Run it: `PENGUWATCH_TOKEN=... python push_ratings.py my-ratings.csv`. A title is found by a catalogue id when you have one
(`{"imdb": "tt0113277"}` is exact), else by its name and year. A rating outside the scale makes PenguWatch refuse the
batch (`422`) and say which item.

## Event webhooks

PenguWatch can call your URL when something happens. Add webhooks on the Settings tab, under Notifications > Event
webhooks (at most ten): a name (it becomes the webhook's id, `Home Assistant` -> `home-assistant`), the events it wants
(all by default), its address and a signing secret. Changes apply at once, no restart.

The secret is how the receiver knows a call is from your PenguWatch: use a long random value (at least 16
characters, `python -c "import secrets; print(secrets.token_urlsafe(32))"`). Type it into the tab, where it is kept in
`secrets.json` and never shown again. Without a secret the calls are unsigned. The address is treated like a password
too: it is stored in `secrets.json`, and is never logged, exported or shown (the System tab and `/api/webhooks` show only
its host). A webhook saved without an address stays off.

**The receiver must run on another address than `localhost`.** PenguWatch refuses to call this machine or a link-local
address (`localhost`, `127.0.0.1`, `::1`, `169.254.x.x`, ...), when the address is saved and again at every call on the
address the name resolves to; LAN addresses such as `http://192.168.1.20:9000/` are fine. To try the example receiver
below, run it on another device, or on this one and enter this machine's LAN address. Calls go out directly: proxy
variables are ignored.

Webhooks in a 0.15 `config.yaml` (with `url`, `name`, `events`, `secret_env`, `secret_file`) are imported once at the first
start of the version with the Settings tab, their addresses moving to `secrets.json`; after that the file is ignored.
An imported `secret_env` (named `PENGUWATCH_WEBHOOK_...`) or `secret_file` (under `/run/secrets/`) keeps working: the
variable or file wins over a typed secret, and if it cannot be read the webhook stays **off** (it never sends unsigned)
and the System tab says why. The tab shows it read-only with **Stop using it**; it cannot name a new variable or file
([Security](security.md#settings-and-secrets)). Settings > Export includes the webhooks without their addresses or
secrets.

### What is sent

A `POST` with `Content-Type: application/json`, one event per request:

```json
{
  "version": 2,
  "id": "6f1c0c1e-0d8e-4c11-9a52-3b2d7f1f4a10",
  "type": "title.status_changed",
  "created_at": "2026-11-04T19:30:12.481516+00:00",
  "data": {
    "title": {"id": 42, "title": "Heat", "year": 1995, "media_type": "movie",
              "ids": {"simkl": 1234567, "imdb": "tt0113277", "tmdb": 949}},
    "from": "plantowatch", "to": "completed"
  }
}
```

`version` is the payload version (2; it changes only for a breaking change, and new fields may appear without one).
Version 2 replaced the title's `simkl_id` with `ids`, the title's ids by provider (as in the API).
`id` is unique per event and also sent as `X-PenguWatch-Delivery`: use it to ignore a repeat. Headers:

| Header | Value |
|---|---|
| `X-PenguWatch-Event` | the event type |
| `X-PenguWatch-Delivery` | the event id |
| `X-PenguWatch-Timestamp` | Unix seconds when this attempt was sent |
| `X-PenguWatch-Signature` | `sha256=<hex>`, only when a secret is set |

| Event | `data` |
|---|---|
| `title.added` | `title` (id, title, year, media_type, ids), `status` |
| `title.status_changed` | `title`, `from`, `to` (statuses) |
| `title.rating_changed` | `title`, `source` (`user` for your rating, or a rating source id), `from`, `to` (numbers or null) |
| `episode.watched` | `title`, `episodes` (up to 100 of `{season, number, code}`), `count`, `watched_at`. Fires for episodes marked in PenguWatch, not for ones that arrive from an account sync. |
| `episode.airs_today` | `title`, `season`, `number`, `episode_title`, `air_date`, `service`, `service_active`. Once per episode, the first time it is seen airing today (checked every 15 minutes). |
| `library.imported` | `count`. The first full sync from the library account (nothing synced before) sends this one summary instead of an event per title. |
| `ratings.imported` | `source`, `updated`, `unchanged`. A bulk `PUT /api/ratings/{source}` sends one per request instead of an event per title. |
| `ratings.cleared` | `source`, `removed`, `source_deleted`. Clearing a source's values or deleting the source. Removing a single title's value sends `title.rating_changed` with `to: null`. |
| `sync.failed` | `error` (text, credentials removed), `trigger`, `at`. Only when the error is new, not on every retry. |

`title.added`, `title.status_changed` and `title.rating_changed` also fire for changes that arrive from the library account in later
syncs. While the very first sync runs, title events are held back (including any you cause by hand in that time) and
`library.imported` is sent at the end. `GET /api/webhooks/events` lists the events with their
meaning.

### Delivery

Delivery runs in the background and cannot slow the app down. Each webhook has its own queue (200 events; when it is
full, new events are dropped and counted) so a receiver that is down delays only itself. A request (connect, send and answer together) is given 10
seconds. **Redirects are not followed** (a redirect is a failed delivery). A `2xx` answer means delivered. Timeouts,
connection errors, `408`, `429` and `5xx` are retried three more times, after 2, 10 and 60 seconds; other `4xx`
answers are final. Events to one webhook are sent in order, but a retried event may arrive after a later one: use
`created_at` or `id` if order matters. Nothing is kept across a restart.

A webhook shows the state `dropping` for ten minutes after its queue was last full. Delivered, failed and dropped counts, the last error and the queue length are on the System tab, at `GET /api/webhooks`
and in `/metrics` (`penguwatch_webhook_deliveries_total`, `penguwatch_webhook_queue_depth`). The last error is one of a
few fixed labels: `redirect not followed`, `refused by the receiver (HTTP 4xx)`, `server error (HTTP 5xx)`, `timed out`
or `could not connect` (no status code, exception name or receiver text: those could repeat the address). `POST
/api/webhooks/{name}/test` (the webhook's name, as on the Settings tab; **Send test** on its card) sends a
`webhook.test` event to see the receiver work.

### Verify the signature

The signature is HMAC-SHA256, keyed with the secret, over the timestamp, a dot and the **exact bytes of the body**:
`sha256=` + hex of `HMAC(secret, "<X-PenguWatch-Timestamp>.<body>")`. Check it before trusting anything in the body,
compare in constant time, and refuse an old timestamp so a captured call cannot be replayed.
[`docs/examples/receive_webhook.py`](examples/receive_webhook.py) is a complete receiver that does this, with only the
standard library:

```python
import hashlib
import hmac
import re
import time


def verify(secret: bytes, headers, body: bytes, now=None) -> bool:
    timestamp = headers.get("X-PenguWatch-Timestamp", "")
    signature = headers.get("X-PenguWatch-Signature", "")
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs((now if now is not None else time.time()) - int(timestamp)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode("utf-8"))
```

Only the timestamp and the body are signed. `X-PenguWatch-Delivery` and `X-PenguWatch-Event` are not, so to ignore a
repeated delivery remember the `id` **inside the body**, and read the event type from the body's `type`.

A known value to test your own implementation against: secret `0123456789abcdef-test-secret`, timestamp `1700000000`,
body `{"version":1,"type":"title.added"}` gives
`sha256=aa6983e669ceeb386f98e2ba091b31b4b3f1e56444cb5a4a6893a02590c00db4`.

Webhook addresses are set by whoever can change settings (with an access token set, only people signed in; without one,
anyone who can reach the port), and PenguWatch calls any address given, including ones on your own network. Point it at
services you trust, and set the access token.

## Add a driver

A **driver** is the one extension point that talks to an outside service: it turns that service's answers into
PenguWatch's neutral types and declares what it offers (a catalogue of titles and episodes, an account's library, an
air schedule, discovery lists, rating sources). Everything else (the page, the API, the sync engine) works on those
types, so a new service is a package under `src/penguwatch/drivers/` and one line in the registry, `DRIVERS`. Drivers
are in-tree: there is no plugin loading, so the dependency checks see everything that runs.

[Drivers](drivers.md#writing-a-driver) is the guide: the terms to check first, the package layout, the class
attributes, each capability and the errors it raises, ids, the rate limiter, built-in rating sources, the credit a
licence may ask for, the guard tests and testing with recorded answers, with a worked example. The TVmaze driver
(`drivers/tvmaze/`) is the model of a small one. For a rating from a source that is not a service PenguWatch talks to
(a spreadsheet, a friend's scores) use [your own rating sources](#bring-your-own-ratings) instead.

## Add a component to the app

Background work that belongs inside the app (like the webhook delivery) is a component: a module with
`async def register(ctx)` and one line in `COMPONENTS` in `src/penguwatch/main.py`. The contract, the order and the
shutdown rules are in [Architecture](architecture.md); the webhooks component (`webhooks_component.py`) is a small,
complete example.
