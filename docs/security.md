# Security

PenguWatch is meant for a home network. It has no user accounts: one shared access token protects it, and it is
built so that a web page you visit elsewhere cannot use your browser to reach it.

## Access token

> [!IMPORTANT]
> Without a token, anyone who can reach the port can read and change your library and your tracker link. The System
> tab shows a warning while no token is set.

What an anonymous client on your network can do in open mode: read and change everything in the library, **connect the
app to their own library-service account** (your later changes are then sent to them), approve and push queued changes, download
a backup of the database, and trigger the webhook and notification tests. Both compose files publish `0.0.0.0:8080`, so
a container is reachable from the whole LAN from its first start: set the token before you first start it, or publish
the port on `127.0.0.1` only. Over plain `http://` the token and the sign-in cookie can be read by anyone on the same
network, so use HTTPS (a reverse proxy) if that matters to you.

Generate a token and put it in `.env` (container) or in `penguwatch.env` next to `config.yaml` (a native install, see
[Install](install.md#settings-and-secrets)), then restart:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# .env
PENGUWATCH_ACCESS_TOKEN=<the token>
```

* **Browsers** get a sign-in page once and then an `HttpOnly`, `SameSite=Strict` cookie that lasts
  `PENGUWATCH_SESSION_MAX_AGE_DAYS` (default 30, at least one hour). Set `PENGUWATCH_COOKIE_SECURE=1` when you serve
  PenguWatch over HTTPS.
* **Signing out:** the Services & Sync tab has a Sign-in card. **Sign out** ends this browser's sign-in (a copy of its
  cookie stops working too). **Sign out everywhere** ends every browser's sign-in at once without changing the token,
  for a lost device. Both are kept in `sessions.json` next to the database (mode 0600, no usable cookie in it).
  Deleting that file undoes them; if it cannot be read, every sign-in is refused until it is fixed, and the log says
  so. Scripts with the bearer token and the calendar feed are not affected. A page left open in a browser whose
  sign-in ended goes to the sign-in page at its next request.
* **Scripts and Prometheus** send `Authorization: Bearer <token>`.
* **Open without the token:** `/api/health` (for uptime monitors), the sign-in page (`/login`), the web app manifest,
  the service worker and `favicon.ico` (they contain no data), the static files under `/static/`, and
  `/api/calendar.ics` with its `?key=`.
* **The calendar feed** (`/api/calendar.ics`) also accepts `?key=<calendar key>`, because calendar apps cannot send
  headers. The key is derived from the token and shown on the Air Schedule tab. Treat that URL like a password: it
  reveals your schedule, the titles you are watching and your services' renewals with their prices (not the rest of your
  library or your tracker login).
* Sign-in attempts are throttled, and changing the token signs everyone out and changes the calendar key.
* After rolling back to a version without sign-out, copied cookies that were signed out work again: change the token
  after such a rollback.

## Always on

* **Host check:** requests whose `Host` header is not an IP address, `localhost`, a single-label name, a name ending
  in `.local`, `.lan`, `.home.arpa` or `.internal`, or one listed in `PENGUWATCH_ALLOWED_HOSTS` are refused
  (protection against DNS rebinding). Add the other names you use, for example `penguwatch.example.net`, or
  `.ts.net` for Tailscale.
* **Cross-site requests** cannot change data: a browser request that changes something must carry the app's own
  `Origin` (or one listed in `PENGUWATCH_ALLOWED_ORIGINS`), and without an `Origin` its `Sec-Fetch-Site` must say
  same-origin. Scripts that send neither header pass this check and rely on the token.
* **Request bodies** are size-capped (`PENGUWATCH_MAX_BODY_BYTES`, default 256 KiB), inputs are validated and bounded,
  and some endpoints (sync, search, backup, diagnostics) are rate-limited.
* **Content Security Policy:** scripts only from the app itself, no inline scripts or event handlers, no plugins, no
  framing by other sites. No inline styles either: `style-src` is the app itself plus the hash of the sign-in page's one style block, with no
  `'unsafe-inline'`. Images may come from any HTTPS host (posters come from the catalogue services' image servers: SIMKL's, with the default driver,
  and TVmaze's when that driver is used).
* **Logs and telemetry** never contain tokens or API keys, and request logs never include query strings (so the
  calendar key is not logged); exported telemetry carries no titles or search text (see [Operations](operations.md)).
* **CSV export** neutralises spreadsheet formulas in titles that come from the catalogue services.

## The container

* Runs as uid 10001, not root, with a read-only root file system, `/tmp` as tmpfs, all Linux capabilities dropped and
  `no-new-privileges` (see `docker-compose.yml`).
* Writes only to `/app/data` (the `penguwatch_data` volume). The database and its WAL files are created with mode
  0600; the backups `scripts/deploy.sh` keeps are private to your user.
* Python packages are installed from hash-locked files and the base image is pinned by digest
  (see [Supply chain](supply-chain.md)).
* An older data volume created by a root-run container needs a one-time `chown -R 10001:0`.

## Limits

* The sign-in throttle and the rate limits work per client address. Rootless Podman's port forwarding does not pass client
  addresses through, so there they apply to all clients together. The token is the real control.
* Behind a reverse proxy, the app sees the proxy's address for every client, because it does not read `X-Forwarded-For`
  (proxy headers are off). Every client behind a reverse proxy on the same host therefore shares one address, and one
  throttle and one set of limits.
* There are no per-user accounts or roles: whoever has the token can do everything, including connecting or
  disconnecting the library service's account and downloading a backup.
* Do not expose PenguWatch directly to the internet. Use a VPN (WireGuard, Tailscale) or a reverse proxy with its own
  authentication if you need it away from home.

## Reporting a vulnerability

Please report it privately through the repository's Security tab (Report a vulnerability) rather than in a public
issue; [SECURITY.md](../SECURITY.md) says how.
