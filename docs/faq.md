# FAQ and troubleshooting

Start with `penguwatch doctor` (native installs; in a container: `docker exec penguwatch penguwatch doctor`). It checks
the port, the config file, the data folder, the time zone and whether each driver's service answers, and says what is
wrong. Logs: see [Install](install.md#troubleshooting).

## The page does not open, or the port is in use

- `penguwatch doctor` reports "FAIL port" when something else holds the port. Pick another one: `--port 8081` when you
  start it, `penguwatch service install --port 8081` for the background service, `server.port` in `config.yaml`, or the
  left side of `8080:8080` in the compose file.
- A native install listens on `127.0.0.1`, this computer only. To reach it from a phone or another computer, see
  [Opening PenguWatch to other devices](install.md#opening-penguwatch-to-other-devices).
- "Host not allowed" (HTTP 421): you reached it by a name it does not know. Add the name to
  `PENGUWATCH_ALLOWED_HOSTS` ([Security](security.md#always-on)).

## The login expired

A banner says "<service> login expired" with a **Reconnect** button (for SIMKL, the default: "SIMKL login expired" and
**Reconnect SIMKL**). Choose it and approve the new code (for SIMKL, at simkl.com/pin). Queued changes stay in the outbox and go out afterwards. PenguWatch normally renews the login by itself;
a login that was pasted in by hand cannot ([Sync](sync.md#login-and-automatic-renewal)).

## Nothing syncs

1. The header pill says what state the library is in. **Services & Sync** shows the last sync, the last error and what is
   waiting.
2. With **review mode** on (the default), nothing is sent or applied until you approve it: look for "N to review" in the
   header ([the review queue](#the-review-queue)).
3. Choose **Sync now**. An error there is shown in the page and logged.
4. Is the library `local`? Then there is nothing to sync, by design ([Drivers](drivers.md#the-local-library-driver)).
5. Can the computer reach the service? `penguwatch doctor` says.
6. Failed pushes retry with a back-off; Services & Sync counts pending and failed ones, and the System tab shows the sync history.

## The review queue

"N to review" counts changes waiting for your decision: yours not yet sent to the library service, and the service's not yet applied here.
**Review pending changes** lists them; approve or decline each, or **Approve everything**. Declining takes the other
side's version. Turn review mode off to sync automatically. Details: [Sync](sync.md#how-the-engine-works).

## A title is on the wrong streaming service

A title's service is matched from its network name against your `services:` list ([Configuration](configuration.md#which-service-carries-a-title)).
Fix it for one title: open its details and change **Streams on** (a manual pick is never overwritten). Fix it for a
whole group, or for one title that streams somewhere other than its network: edit `config.yaml`, add the network name
to the right service's `aliases`, or the exact title to its `titles` (a **title rule**, which wins over the network),
and restart. The **Tidy** button lists titles with no service.

## The time zone is wrong

"Today", renewal dates and the daily notification hour use `server.timezone` (or `PENGUWATCH_TZ`, else the `TZ`
variable, else UTC). Set an IANA name such as `Europe/Berlin` and restart; `penguwatch doctor` says whether the name is
known.

## Behind a reverse proxy

- Add the public name to `PENGUWATCH_ALLOWED_HOSTS`, and, when the proxy's public name differs from what the app sees,
  the public origin to `PENGUWATCH_ALLOWED_ORIGINS` (for example `https://penguwatch.example.net`).
- Serve it over HTTPS and set `PENGUWATCH_COOKIE_SECURE=1`.
- Set the access token. The app does not read `X-Forwarded-For`, so every client looks like the proxy to the sign-in
  throttle and rate limits ([Security](security.md#limits)).

## Back up and restore

`penguwatch backup` copies the database and the sessions file into a timestamped folder (container:
`docker exec penguwatch penguwatch backup`; system service: `sudo ... backup --system --to DIR`, see
[Install](install.md#backing-up-the-data)). The copy includes the account login: keep it private. To restore, stop
PenguWatch, put the file back as `penguwatch.db` (delete `penguwatch.db-wal` and `-shm`) and start it.
`GET /api/export/backup.db` downloads a copy without the account login, and `GET /api/export/library.csv` your library as
a spreadsheet.

## Upgrade

The install script again (it backs up first), `uv tool upgrade penguwatch` followed by `penguwatch service install --user`,
or a new tag in `compose.yaml` and `docker compose up -d`. Back up first. Migrations run on start. See
[Install](install.md#upgrade).

## Roll back

Migrations only go forward, so an older version may not read a database a newer one has changed. Roll back with the
backup taken before the upgrade: stop PenguWatch, install the old version (`install.sh --version X.Y.Z`,
`uv tool install penguwatch==X.Y.Z`, or the old image tag), restore the backup as above and start it. Changes made
after the backup are lost.

## Can I use it without an account?

Yes: [the README says what you give up](../README.md#without-an-account).

## Where is my data?

In one SQLite database: `penguwatch paths` prints where; in a container it is the `penguwatch_data` volume. Nothing
else is stored. The only services it talks to are the ones your `drivers:` name.
