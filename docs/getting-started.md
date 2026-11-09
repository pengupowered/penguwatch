# Getting started

The first run, from nothing to a library you can use. It takes about ten minutes. Two routes: **with a library service** (your
library lives in an account at a tracking service and stays in step with it; SIMKL is the one that ships and the
default, so the steps below use it) or **without an account** (local library and TVmaze). Pick one at step 4; the
other steps are the same.

## 1. Install

Pick the one-liner for your system from the [README](../README.md#quick-start), or the longer
[Install](install.md) guide. In short:

- **Windows:** `irm https://github.com/pengupowered/penguwatch/releases/latest/download/install.ps1 | iex`
- **macOS and Linux:** `curl -fsSL https://github.com/pengupowered/penguwatch/releases/latest/download/install.sh | sh`
- **uv or pipx:** `uv tool install penguwatch`, then `penguwatch`
- **Container:** the compose file in the README, then `docker compose up -d`

A native install runs in the background, starts by itself and listens on this computer only. A container publishes
port 8080 on your whole network, so **set an access token first** if other people use that network (step 6).

## 2. Open the page

Go to `http://127.0.0.1:8080` (for a container on another machine: `http://<that machine's address>:8080`). The
install script opens it for you. You see the Up Next tab, empty for now.

If it does not open, [the FAQ](faq.md#the-page-does-not-open-or-the-port-is-in-use) has the usual causes.

## 3. Names and services

Open the **Settings** tab and, under Household, enter the names of the people who watch (Add person for each one). With
two or more, every title says who wants to watch it. Everything else on the tab has a sensible default; [Configuration](configuration.md) lists every setting.

Then open **Services & Sync** and switch on the services you pay for this month, with their prices and renewal days: the
optimizer needs this. The tab starts with a built-in list of common US services, with their list prices and the network
names each one carries; edit the names, prices and aliases for where you live. Prices you change are never overwritten.

(Before this version these came from `config.yaml`. If you are upgrading, the first start reads that file once and moves
its values onto the tabs; after that the file only sets the listen address and the database location.)

## 4. Choose where your library lives

### With a library service (SIMKL, the default)

The steps below connect SIMKL, the built-in library service. Another library driver would bring its own connect step
([Drivers](drivers.md)).

1. Create a free SIMKL app once at [simkl.com/settings/developer](https://simkl.com/settings/developer/app/new). Fill in
   the form (SIMKL says what it asks for) and copy the **client ID** of the app it creates. That is the "app id" that identifies
   PenguWatch to SIMKL.
2. In PenguWatch, open **Services & Sync**, choose **Enter client ID / token manually**, paste the client ID and save.
3. Choose **Connect SIMKL with Device PIN**. PenguWatch shows a code and a link: open it at simkl.com/pin, sign in and
   enter the code. PenguWatch waits for the approval and then starts the first sync.

### Without an account (local library plus TVmaze)

On the Settings tab, under **Data sources**, set Library to Local, Catalogue to TVmaze, Schedule to TVmaze and Discovery to
none, save and restart PenguWatch (data sources apply after a restart). Then add titles with **Add title**, searching TVmaze. There is nothing to connect. The trade-offs (no movies, no
Discover, no sync to another device) are listed in the [README](../README.md#without-an-account).

## 5. The first sync

With a library service, the first sync starts right after you connect: it reads your whole library once. **Review mode is on by
default**, which changes what you see:

- The first import into an empty library is applied directly, so your titles appear without a question.
- After that, nothing is sent to the library service and nothing from it is applied until you approve it. The header shows
  "N to review"; open **Services & Sync** and **Review pending changes**, or choose **Approve everything**. Turn the
  setting off ("Ask me before syncing changes") when you trust it.
- Details (episode lists, English titles, ratings, the air schedule) are filled in over the next sync cycles, a few
  titles at a time, so a big library takes a while to complete. **Sync now** forces a pass.

Then look at: **Up Next** (the next episode of everything you watch; the check mark marks it watched), **Library**
(statuses, services and ratings), **Optimizer** (what to keep, cancel or rotate to) and **Air Schedule**. The **Tidy**
button in the Library lists titles with no service so you can assign one.

With a local library there is nothing to review: your changes are saved at once.

## 6. Protect it

Before anyone else on your network can reach the port, set `PENGUWATCH_ACCESS_TOKEN`: see
[Install](install.md#settings-and-secrets) and [Security](security.md). Browsers then ask for it once.

## Where next

- [Using PenguWatch](usage.md): what each tab does.
- [FAQ and troubleshooting](faq.md): when something does not behave.
- [Operations](operations.md): backups and notifications.
