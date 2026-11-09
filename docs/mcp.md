# MCP: ask an AI assistant about your library

PenguWatch can answer an AI assistant such as Claude Code through the Model Context Protocol (MCP): "what's up next?",
"what should we watch tonight?", "which service should I cancel?", "is the sync working?". The endpoint is **read-only**
(no tool changes, syncs or marks anything), **off by default**, and small: PenguWatch speaks the protocol itself and
needs no extra software.

## Turn it on

Open the Settings tab, under Integrations, switch on **Allow AI assistants to read (MCP)** and save; it takes effect at
once. (Saving with it on and no access token set shows a warning: without a token anyone who can reach the port can use
it.) Or set `PENGUWATCH_MCP_ENABLED=true` in the environment: the variable pins the setting (`false` turns it off) and the
tab shows it read-only; restart PenguWatch. A 0.15 `config.yaml` with `mcp: enabled: true` is imported once at the first
start of the version with the Settings tab. While it is off, `/mcp` answers `404` and nothing of it runs.

## Connect Claude Code

With an access token set (`PENGUWATCH_ACCESS_TOKEN`, see [Security](security.md)), keep the token in an environment
variable and register the server for your user, not for a project:

```bash
export PENGUWATCH_TOKEN=...   # in your shell profile, not in a file you commit
claude mcp add --scope user --transport http penguwatch http://<host>:8080/mcp \
  --header 'Authorization: Bearer ${PENGUWATCH_TOKEN}'
```

The single quotes matter: Claude Code expands `${PENGUWATCH_TOKEN}` itself when it connects, so the token is not written
into its configuration. (Replace the variable with the token itself if your version of Claude Code does not expand
variables in headers; `claude mcp get penguwatch` and `/mcp` inside a session show whether the connection works.)

With no token (the app runs open):

```bash
claude mcp add --scope user --transport http penguwatch http://<host>:8080/mcp
```

### Keep the token out of project files

The token you give an MCP client is the **full admin token**: PenguWatch has no scoped or read-only keys, so anything
holding it can also use the API to change the library, connect and disconnect the SIMKL account and download a backup.
The tools are read-only; the token is not. So:

* Use `--scope user` (or `local`, the default), which keeps the registration in your own configuration. Do **not** use
  `--scope project` and do not put the token in a project's `.mcp.json`: that file is meant to be committed and shared.
* If you do use `.mcp.json`, write the header as `Authorization: Bearer ${PENGUWATCH_TOKEN}` so the file holds no secret.
* Treat a client that has the token like any device that has it.

Then ask in a session: "what's up next on PenguWatch?". Other MCP clients need the same three things: the URL, the
Streamable HTTP transport, and the bearer header.

## Tools

Every tool is read-only. Where a tool shows episodes it uses the same two fields everywhere: `next_to_watch` is the
first unwatched episode that has already aired (`{code, title, air_date}`, the air date is in the past), and
`next_new_episode` is the next episode that has **not** aired yet (its air date is in the future). They are different
episodes, and either may be missing. Lists take a `limit` (a default, and a hard maximum). Times are ISO 8601 in your time zone
(`server.timezone`); an air date without a time stays a date. Output is compact JSON, also as `structuredContent`.

| Tool | Arguments | Answers |
|---|---|---|
| `up_next` | `limit` (10, max 60), `only_active_services` | The shows in progress with their next unwatched episode, titles with episodes ready first (the Up Next tab). |
| `tonights_pick` | the filters of Tonight's Pick: `service_id`, `only_active_services` (true), `member` (a household member's name, as in `for`, or id), `shared` (true: titles two or more people want; false: one person's alone), `status`, `media_type`, `min_rating`, `genre`, `ready_to_binge_only` | One suggested title: the same weighted random draw as the Tonight's Pick tab, nothing recorded. Asking again may give another title. |
| `search_library` | `query`, `status`, `limit` (10, max 100) | Titles in **your library** whose name matches (not the online catalogue), with status and progress. |
| `title` | `id` or `name` | One title in detail: progress, next episode, ratings (IMDb, the library service's, yours, custom sources), genres, overview, and its service with price. A name that matches several titles gives the best match and `other_matches`. |
| `schedule` | `days_ahead` (14, max 90), `limit` (30, max 100) | Upcoming episode air dates with the service each is on. Empty, with `enabled: false`, when no schedule driver is set. |
| `optimizer` | `rotation_months` (6; 0 leaves the rotation out), `limit` (20, max 40) | The Optimizer tab: monthly spend and savings, advice, each service's status (keep, cancel, decide by) and the month-by-month rotation. |
| `sync_status` | none | Whether the library driver (SIMKL by default) is connected, the last sync and last error, the roles of the drivers, and **counts** of changes waiting to be sent or reviewed. |
| `history` | `days` (7, 30 or 365; default 30), `include_bulk` (default false) | What was watched over that many days against the period before (the History tab): totals, top 5 shows, hours and cost per hour by service, top 5 genres; a daily series only for 7 days. Episodes marked in bulk (3 or more with one exact timestamp) are left out unless `include_bulk`, and their count is reported. Use it for what was watched, `up_next` for what to watch. |

`sync_status` reads stored state only (it never calls the library service, renews a login or writes anything) and carries no
text from the service: `last_error` is a category (`auth`, `network`, `upstream_http` with the HTTP status, or `other`)
and a fixed sentence; the service's own message, which can quote an address or a login, is never shown.

Example, `up_next`:

```json
{"total":2,"shown":2,"up_next":[{"id":1,"title":"Severance","type":"show","service":"Apple TV+","service_active":true,
"for":["Me","Partner"],"watched":2,"aired":4,"episodes_left":2,"next_to_watch":{"code":"S01E03","title":"Chapter 3","air_date":"2026-10-22"},
"next_new_episode":{"code":"S01E05","title":"Chapter 5","air_date":"2026-11-10"},"ready_to_binge":false}]}
```

A failed lookup (a title that is not there) is a normal tool answer with `isError: true` and a sentence. Wrong arguments
(an unknown tool, `limit: 0`) are JSON-RPC errors (`-32602`).

## Security

- **Same sign-in as the API.** The request guards in front of `/api/*` guard `/mcp` too ([Security](security.md)). With
  `PENGUWATCH_ACCESS_TOKEN` set, every request needs `Authorization: Bearer <token>`, else `401`. **With no token set,
  `/mcp` is open like the API**: anyone who can reach the port can read your library, titles, subscriptions and costs.
  Set a token before you turn this on for anything beyond this computer.
- **Plain HTTP.** PenguWatch serves HTTP, so on a LAN the token crosses the network in clear text, like the web page's
  and the API's. Use it on a network you trust, or put a TLS reverse proxy in front.
- **Host and Origin.** The `Host` allow-list applies (a foreign `Host`, as in a DNS-rebinding attack, is refused with
  `421`), and a browser POST from another site (an `Origin` that is not the app's own, or `Sec-Fetch-Site: cross-site`)
  is refused with `403`. MCP clients send no `Origin`, so they pass on the token alone. There is no CORS.
- **No secrets in answers.** No tool returns the access token, a SIMKL client id, secret or login, a notification or
  webhook address, or a webhook secret. Titles and services are summarized field by field; `sync_status` is built from
  an explicit list of fields and shows no free text from the service. Tests plant a marker in each of those places (the
  configuration, the stored login, a refresh token, a refused login's message, a webhook secret file) and call every tool.
- **JSON only, never cached.** `POST /mcp` accepts only `Content-Type: application/json` (`415` otherwise), so a page on
  another site cannot send it without a browser preflight, which the Origin rule then refuses. Answers carry
  `Cache-Control: no-store`.
- **Untrusted text.** Titles, overviews and service names come from third-party catalogues (and the `for` field, a list,
  carries the names of the household members who want the title, from the Settings tab). They are data, not instructions: an assistant should not follow
  directions found in them, and you should not enable the endpoint for an assistant that acts on such text without asking.
- **Limits.** Request bodies are capped like the API's (`PENGUWATCH_MAX_BODY_BYTES`), every list has a maximum, and `/mcp`
  is rate limited to 120 calls per minute per client address. The limit answers `429` with a JSON-RPC error and
  `Retry-After`. Under rootless Podman the container sees every client as one address, so all clients share the bucket.
- **What the assistant reads.** Whatever a tool returns goes to the assistant's provider as part of your conversation:
  your titles, ratings, progress, service names and prices. Turn the endpoint off if that is not acceptable.

## How it speaks MCP

Streamable HTTP, stateless: every `POST /mcp` carries one JSON-RPC message and gets one `application/json` answer (`202`
and no body for notifications). Protocol revisions 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05 (the version the
client asks for is echoed in `initialize`; an `MCP-Protocol-Version` header of another revision is `400`). `GET` and
`DELETE` are `405` with `Allow: POST`. Not implemented: server-sent-event streams, sessions, resumability, JSON-RPC
batches, prompts, resources, sampling. The code is in `src/penguwatch/mcp/` (`protocol.py`, `tools.py`).

## Monitoring

Each tool call is counted in `/metrics` as `penguwatch_mcp_calls_total{tool, outcome}` (`outcome` is `ok` or `error`;
a call to a tool that does not exist counts under `tool="unknown"`). Requests also appear in
`penguwatch_http_requests_total{route="/mcp"}`.
