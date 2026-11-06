# Sync

PenguWatch keeps its library in step with an account through the **library driver** ([Drivers](drivers.md)). The sync
engine (`src/penguwatch/sync/`) is the same for every driver: it owns the outbox, review mode, back-off, the pull
cursor and applying changes, and the driver only talks to its service. The default library driver, and so the default *library service*, is SIMKL, described
in [the second half of this page](#simkl); with `drivers: {library: local}` there is no account and nothing here applies
([The local library driver](drivers.md#the-local-library-driver)).

## How the engine works

| Direction | What | How |
|---|---|---|
| PenguWatch to the account | episodes watched or unwatched, a movie watched, status changes, ratings set or removed, titles added, titles removed | Each change is written to the persistent **outbox** as a neutral operation (`SyncOp`) for the library driver, which turns it into its service's request. Once approved (immediately when review mode is off) a background worker delivers it within seconds. Failures retry with back-off (service down, rate limit, restart), in order per title; operations the driver says may share a request (`batch_key`) go out together. A refusal the service calls permanent is dropped after three tries, and "already there" counts as done. Any other error a driver raises counts as a transient one (logged with its traceback, the row backs off). When it happens to a batch, nothing says whether the request went out, so none of its rows is resent at once: all back off and each is sent alone on its next try. Pending items are shown in *Services & Sync*. |
| The account to PenguWatch | status, progress, watched history, ratings, removals, new titles | The driver plans a pull from the cursor the engine stored after the last one (`plan_pull`), then yields the changes a part at a time (`pull`). The engine applies each part before the next is fetched and stores the new cursor only once everything was applied or staged. |

**Review mode (on by default):** nothing is sent and nothing from the account is applied until you approve it. Your
local changes wait in the outbox as *awaiting approval*, and changes found on the account are staged for review (under
*Review Changes* in *Services & Sync*); approve or discard them there. Turning review mode off only affects new changes:
those go out (and are applied) automatically, while changes already waiting for approval stay unapproved until you
approve or discard them. The very first import into an empty library is applied directly.

**Conflict rule:** while a title has an unsent local change, pulls don't overwrite its status, progress or rating, so a
sync that races your click can't undo it. Household fields (who wants to watch, priority, notes, manual service) are
local only and never touched by pulls.

**Switching to the local library and back:** while the library is local (`drivers: {library: local}`) nothing is sent
or pulled, but an edit to a title the last account driver knows (watched, status, rating, delete) is kept in the
outbox for it as an *unapproved* row, even with review mode off. After switching back, those rows count as unsent
changes (a pull leaves the title alone and does not bring a deleted title back) and are listed under *Review Changes*
(also with review mode off): approve to send them, decline to take the account's version. Nothing is sent without
that. Titles without the account driver's id, and titles added while local, get no row
([Drivers](drivers.md#switching-the-library-driver)).

**Newer changes replace older unapproved ones:** when a change is queued, the older *unapproved* rows of the same title
that it contradicts are deleted, so approving old rows later cannot undo a newer change: a status replaces older status
rows (and an older unapproved removal: the title was added again); a rating or "remove rating" replaces older rating rows; a removal replaces every older unapproved row of the title;
episodes watched or unwatched are taken out of the older unapproved watched/unwatched rows (a row left with no episodes
is deleted, a trimmed row's description is rewritten; a movie's watched change replaces its older ones). Approved rows are
never touched: they go out first, in order per title. Rows queued before the driver layer (with a body in the service's own
format, "native") are replaced whole where a whole-row rule applies (status, rating, removal); a native watched/unwatched
row cannot be trimmed, so it stays and the newer watched/unwatched row is made unapproved too, for you to decide, so
that the order cannot invert.

**Titles added before the login ("add if missing"):** a title added from the catalogue while the library driver has an
account that is not connected is queued with an *if missing* mark (the add), and so is any later edit of that title
before the full pull below (an *edit* mark). An edit of a title that was already here (from an earlier sync) is not
marked: it goes out as usual once connected, so an edit made while logged out is never dropped. Marked rows are never
delivered before a **full** pull with the account's listings has run; the engine plans one when a pull is due (the
worker's normal interval, a manual sync, the sync after logging in), not on every wake. Nothing is decided while the pull
runs: only when **every** listing of the pull (all media types) has succeeded, just before the cursor is saved, are the
marked rows resolved, by key across media types. A pull that fails anywhere leaves them all as they were.
- The account has the title: its state (status, watched episodes, rating) comes in as in any pull (with review mode on, as
  an incoming change to approve) and the marked **add** is dropped. Later **edits** are not dropped: they are kept,
  unmarked and *unapproved*, so you decide in *Review pending changes*: approve sends the edit, decline takes the
  account's version.
- The account lacks the title: the marks are cleared and the add and the edits go out like any others (waiting for
  approval when review mode is on).

The pull never removes these titles. A **delete** made while not connected is not marked: it is queued as asked (the
confirm dialog asked for it) and replaces the older rows of the title.

**Deciding on rows kept for an inactive driver:** *Decline* takes the account's version, so it is refused (HTTP 409, and the
button is disabled with the reason) for rows of a driver that is not the connected library driver: "Switch the library back
to X and connect to decide on these.", or "Connect X to decide on these." when X is the library driver but not connected. *Approve* is still allowed; the row waits for that driver. Rows that cannot be declined can still be removed with
*Drop without restoring* (`POST /api/sync/outgoing/discard` with `restore: false`): it deletes the rows, fetches nothing and
leaves the local state as it is until a later sync brings the account's version of those titles; a confirmation says so. It works for rows of an inactive driver, of a driver that is no
longer configured, and for rows from while the library is disconnected.

**Removals:** a pull that lists the account's whole library for a media type removes the local titles of that type that
are missing from it, unless they are waiting to be pushed, have no id of the library driver (titles added through a
catalogue only) or carry the `local` id (added while the library was local). In review mode a removal is staged for
you to approve. A listing that comes back empty while more than five titles of that type are held locally is treated
as a failed answer, and nothing is removed.

**Enrichment:** separately from the library, the engine refreshes each title's details, episode list, sequels and
ratings from the **catalogue** drivers (the library driver's own catalogue answers for a title that has its id, so
episode numbers stay those of the account; otherwise the first that knows the title's id answers; one title at a time, a pause between
calls, a limit per cycle and a 24 h back-off per title after a failure), and the air schedule from the **schedule**
drivers. These run in every mode, with or without an account.

The first sync after connecting (or **Full Re-sync**) reads the whole library once. After that, syncs are deltas.

## SIMKL

SIMKL is the default library driver (`drivers/simkl/`). You need your own SIMKL app (free, at
[simkl.com/settings/developer](https://simkl.com/settings/developer/app/new)); its client ID identifies PenguWatch to
SIMKL.

The driver follows SIMKL's recommended two-phase sync: `GET /sync/activities` every 30 min (and on **Sync Now**); if
nothing changed, nothing else is fetched. Otherwise only the moved buckets are fetched with `date_from` deltas, ratings
via `/sync/ratings`, removals via an id diff. Pushes are SIMKL's `/sync/history`, `/sync/add-to-list`, `/sync/ratings`
and their removal counterparts.

### API usage

PenguWatch follows SIMKL's [Sync guide](https://api.simkl.org/guides/sync) and keeps its API use low:

* **Schedule:** a check every 30 min while the app is in use, every 3 h after 12 h without use
  (`auto_sync_interval_minutes`, `idle_after_hours`, `idle_sync_interval_minutes` in `config.yaml`).
  The open page refreshing itself does not count as use; the next real interaction triggers one sync.
* **Each cycle** makes one `/sync/activities` call, and fetches lists only for the buckets that changed
  (with `date_from`). Changes waiting in review are not re-fetched until SIMKL moves again.
* **Per-title refreshes** (details, episode lists, sequels) run one at a time, 0.5 s apart, at most 25
  titles per cycle; the rest follow on later cycles.
* **Pushes** go out immediately once approved, batched, regardless of idleness.

English and alternative titles (`en_title`, `alt_titles`) are read during the normal enrichment, with no extra SIMKL
calls. Titles whose alternative titles were never fetched are filled in over the following sync cycles (anime first)
under the same limits (25 per cycle, 0.5 s apart, 24 h back-off); the log says how many remain. The add-title search
retries once with the accent-stripped query when an accented one finds nothing.

### Login and automatic renewal

The Device PIN login normally gives PenguWatch a short-lived access token plus a refresh token, and it renews the
access token by itself (a day before it expires, and on the first rejected call), so you should not have to log in
again. What *Services & Sync* and the header show:

| State | Meaning | What you see |
|---|---|---|
| **Renews automatically** | A refresh token is held and SIMKL accepts it. | Nothing to do. |
| **Automatic renewal failed** | SIMKL refused the refresh token, but the current access token still works. | A note on *Services & Sync* with the expiry date; no banner. |
| **Cannot renew automatically** | The login has no refresh token (a pasted token, or the classic PIN flow, which does not issue one). | Noted on *Services & Sync*. If SIMKL reported an expiry, a banner appears in the last day ("expires … and can't renew automatically"). |
| **Needs reconnect** | The login is not working and cannot renew itself (a call returned 401 and there is no usable refresh token). | An app-wide banner with a **Reconnect SIMKL** button. The background worker stops pushing and pulling (queued changes stay in the outbox) and tries SIMKL once an hour, so a token that works again clears the banner. A manual **Sync Now** always tries. |

SIMKL being unreachable, rate limiting, or an error page from its CDN is treated as a passing problem and retried
with a back-off; only an OAuth refusal of the refresh token counts as refused. A new login (PIN, pasted token) or
**Disconnect** replaces the stored refresh token, and the answer to any renewal still in flight is discarded. The
banner clears as soon as an authenticated call succeeds.

SIMKL credentials (client ID, access and refresh token) are stored in the database, which is created readable only by
the app's user. They are removed from the backups you download (see [Operations](operations.md)).
