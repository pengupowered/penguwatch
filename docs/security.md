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

## Settings and secrets

Settings are changed on the Settings tab, through `/api/settings*`. These routes need the access token like every
other `/api` route, and are never open: **with a token set, whoever can sign in can change settings; without one,
anyone who can reach the port can**, exactly as they can already change your library. What the settings add to that is
the ability to point the daily digest and the event webhooks at any address, so the tab shows the no-token warning at its
top. Set the token.

* **Secrets file.** The digest address and each webhook's address and signing secret are kept in `secrets.json` next to
  the database (`/app/data/secrets.json` in the container): mode 0600, written atomically, in the 0700 data folder. They
  are **write-only**: no API answer, page, Export, saved version, log line, error message or telemetry attribute carries
  one (the tab shows "set" and the address's host). The SIMKL login stays in the database, as before.
* **Back it up with the database.** `penguwatch backup` and `scripts/deploy.sh` copy it (privately). The downloadable
  `backup.db` is deliberately without it, so that file stays safe to keep anywhere; see [Operations](operations.md#backups-and-exports).
* **Pinned by the environment.** A secret or setting set by an environment variable (for example
  `PENGUWATCH_NOTIFY_URL`) cannot be changed through the API; a write is refused with the variable's name.
* **Webhook restrictions.** A webhook's signing secret is signed into every delivery it makes, so the page and the API
  never let a caller choose where that secret comes from: a save that sets a new or changed `secret_env` or
  `secret_file` is refused. One imported from a 0.15 `config.yaml` (which the import keeps only when it is named
  `PENGUWATCH_WEBHOOK_...` or is a path under `/run/secrets/`) can be kept or cleared, nothing else, and while a webhook
  still reads it, its address can't be changed either. Without that, anyone able to write settings could have the server
  sign with a secret of their choice (`PENGUWATCH_ACCESS_TOKEN`, `/etc/passwd`, ...) and send the result to their own
  address, to guess it offline. Webhook items are matched to the stored ones by id, never by name. A typed signing secret
  must be at least 16 characters.
* **Addresses (SSRF).** A webhook address and the digest address must be `http` or `https` with a host and a valid
  port, without `user:password@`, and not this machine or a link-local address: `localhost`, loopback, `0.0.0.0`,
  link-local (`169.254.x.x`, the cloud metadata address included), multicast, IPv4-mapped forms and numeric spellings such
  as `2130706433` or `0x7f.1` are refused when saved. Every call checks again, on the address the name resolves to, and
  connects to that checked address (TLS still verifies the name), so a name that later points at this machine (DNS
  rebinding) gets nothing. This also covers `PENGUWATCH_NOTIFY_URL` and imported webhooks. LAN addresses stay allowed.
  Redirects are not followed, and the two HTTP clients ignore proxy variables, so nothing routes around the check.
* **Coarse errors.** A failed webhook or digest call is recorded as `redirect not followed`, `refused by the receiver
  (HTTP 4xx)`, `server error (HTTP 5xx)`, `timed out` or `could not connect`, never with the receiver's text or the
  exception's message; a failing settings applier is logged with its exception type and frames, never its message.
* **Errors** name the field, never repeat what you typed.
* **Settings are rate-limited** (saves, validation and import) and subject to the same Host, Origin and body-size checks
  as every write.

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

## The MCP endpoint

`POST /mcp` ([MCP](mcp.md)) is **off by default** (`mcp.enabled`). When on, it is guarded exactly like the API: the Host
check, the cross-site rule, the body cap and the access token (with no token set it is open, like the API). It accepts
only `application/json` bodies and its answers are never cached. Its tools only read, and no tool returns a secret, but:

* **The token is the full admin token.** There are no scoped keys: a client that holds it can also write the library,
  connect and disconnect the library service's account and download a backup through the API. Keep it out of project
  files (see [MCP](mcp.md#keep-the-token-out-of-project-files)).
* **Answers go to the assistant's provider.** Whatever a tool returns (titles, progress, ratings, service names and prices)
  is sent to the AI service you use as part of your conversation.
* **The token crosses the network in clear** over plain HTTP, as on the web page.
* **Rate limit:** 120 calls a minute per client address, answered with `429` as a JSON-RPC error; under rootless Podman
  every client shares one address (see Limits).

## The container

* Runs as uid 10001, not root, with a read-only root file system, `/tmp` as tmpfs, all Linux capabilities dropped and
  `no-new-privileges` (see `docker-compose.yml`).
* Writes only to `/app/data` (the `penguwatch_data` volume). The database and its WAL files are created with mode
  0600, and so is `secrets.json`; the backups `scripts/deploy.sh` keeps are private to your user.
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
