# Install

PenguWatch is one web app with one SQLite database. It runs natively on Windows, macOS and Linux (including a 64-bit
Raspberry Pi), or in a container. Pick the section for your computer: [Windows](#windows), [macOS](#macos),
[Linux](#linux), [Raspberry Pi](#raspberry-pi) or [Container](#container). Every native install ends the same way:
PenguWatch runs in the background, starts by itself, listens on this computer only (`http://127.0.0.1:8080`), and its
page opens in your browser. What you do next is in [First run](#first-run).

## What has been tested where

- Linux, x86-64: The installer (`install.sh`, without the service step) ran in a throw-away Debian container on a real
  Linux computer: it fetched the pinned uv, installed Python 3.12 and the locked dependencies, the installed program
  answered `/api/health`, upgraded in place (with a backup) and uninstalled, keeping the data. The systemd units are
  checked by unit tests and by `systemd-analyze security --offline` (about 1.2, "OK"), and the container image on the
  same computer. A running systemd service is not yet tried by hand
- Linux, GitHub's runners: The CI job `install` passes on an Ubuntu runner (`ubuntu-24.04`): it installs the system
  service with `install.sh` from the release files, checks that it answers and who it runs as, that the settings file
  reaches it (with an access token the API refuses), upgrades (a backup is made) and uninstalls (nothing is left)
- Linux, per-user service (`--user`): Only its unit text and the installer's `--user` layout are tested; it is not started
- Raspberry Pi, 64-bit: Not on a Pi. It is the Linux arm64 install: the CI job `native` installs the wheel and runs it on
  GitHub's arm64 runner (`ubuntu-24.04-arm`), and the arm64 image is built on a native arm64 runner; both pass
- macOS: Not tried on a real Mac. The CI job `install` passes on GitHub's macOS runner (`macos-latest`, Apple silicon):
  it installs the LaunchDaemon with `install.sh`, runs the same checks, upgrades and uninstalls; the wheel is also
  installed and run natively there. The `--user` LaunchAgent has only its plist text tested
- Windows: Not tried on a real Windows PC. The CI job `install` passes on GitHub's Windows runner (`windows-latest`): it
  installs with `install.ps1`, checks that the logon task answers, upgrades and uninstalls; the wheel is also installed
  and run natively there
- Container: The image is tested read-only, as uid 10001, on the Linux runner and on a real Linux host. Docker Desktop
  on macOS or Windows is not tested

If something differs on your Mac or Windows computer, that is a bug worth reporting.

The install scripts are small and readable: [`install.sh`](../scripts/install.sh) (Linux, macOS) and
[`install.ps1`](../scripts/install.ps1) (Windows). They:

1. download the wheel, the dependency lock and `SHA256SUMS` from the [GitHub release](https://github.com/pengupowered/penguwatch/releases)
   and check the wheel and the lock against `SHA256SUMS`;
2. download [uv](https://docs.astral.sh/uv/) at a version and sha256 pinned in the script, and check it;
3. let uv install Python 3.12 (kept inside PenguWatch's own folder, nothing is changed in your system Python) and create a
   virtual environment;
4. install every dependency from the lock with `uv pip install --require-hashes --only-binary :all:`, so each file must
   match its recorded hash and nothing is compiled, then install the wheel;
5. run `penguwatch service install`, wait until `/api/health` answers, and open the browser.

The lock is the same file the container image is built from (`requirements.txt`, one lock for every operating system,
with markers for the packages only some systems need, such as the time zone database on Windows).

Options (the same names in both scripts, `-Version` style on Windows): `--version X.Y.Z` (default: the latest release),
`--host ADDRESS` (`-ListenHost` on Windows; default this computer only, see
[Opening PenguWatch to other devices](#opening-penguwatch-to-other-devices)), `--port N` (default 8080), `--user`
(Linux, macOS: no sudo) or `--system` (the default as root), `--no-service` (install only, start it yourself with `penguwatch serve`), `--no-browser`,
`--yes` (do not ask), `--from DIR` (install from a folder with the release files, for a computer without internet
access), `--uninstall`.

## Windows

**Needs:** 64-bit Windows 10 or 11 with Windows PowerShell 5.1 or newer (already there), and internet access to GitHub
for the download. No administrator rights. In PowerShell:

```powershell
irm https://github.com/pengupowered/penguwatch/releases/latest/download/install.ps1 | iex
```

Or download `install.ps1` first, read it, and run `powershell -ExecutionPolicy Bypass -File install.ps1`.

What you get:

- The program in `%LOCALAPPDATA%\Programs\PenguWatch`, your data (database, sessions, `penguwatch.log`) in
  `%LOCALAPPDATA%\PenguWatch`, and `config.yaml` and `penguwatch.env` in `%APPDATA%\PenguWatch`.
- A Task Scheduler task named `PenguWatch` that starts PenguWatch every time you sign in, without a console window
  (`penguwatchw.exe`), and restarts it if it stops. It is not a Windows service: it runs while you are signed in, as you.
  Windows may show a firewall prompt only if you change the listen address from `127.0.0.1`.
- The time zone database comes from the `tzdata` package (Windows has none of its own).

Check it with `penguwatch service status` (the script prints the full path of `penguwatch.exe`; add the folder
`%LOCALAPPDATA%\Programs\PenguWatch\venv\Scripts` to your PATH if you want the short command). The windowless run keeps
its log in `%LOCALAPPDATA%\PenguWatch\penguwatch.log`.

## macOS

**Needs:** macOS on Apple silicon or Intel, the Terminal's `curl` and `sh` (both ship with macOS), internet access to
GitHub, and for the default install an administrator account (for sudo). In Terminal:

```sh
curl -fsSL https://github.com/pengupowered/penguwatch/releases/latest/download/install.sh | sh
```

By default this installs a **system service** and asks for your password once (sudo), after saying why: it puts the
program in `/usr/local/penguwatch`, creates a hidden account `_penguwatch` with no login, and adds a LaunchDaemon that
runs PenguWatch as that account from boot, before anyone signs in. Data, `config.yaml`, `penguwatch.env` and
`penguwatch.log` are in `/Library/Application Support/PenguWatch`, readable by that account only.

Without sudo: add `--user`. That installs a LaunchAgent under your own account (program in
`~/Library/Application Support/PenguWatch/app`, data, config and settings in `~/Library/Application Support/PenguWatch`,
log in `~/Library/Logs/PenguWatch`) that starts when you sign in.

## Linux

**Needs:** a systemd-based distribution on x86-64 or arm64, `curl` and `sh`, internet access to GitHub, and for the
default install `sudo`. In a terminal:

```sh
curl -fsSL https://github.com/pengupowered/penguwatch/releases/latest/download/install.sh | sh
```

By default this installs a **system service** and asks for your password once (sudo), after saying why: it puts the
program in `/opt/penguwatch` (owned by root, so the service cannot change its own code) and adds a systemd unit
`penguwatch.service`. The unit runs PenguWatch as a throw-away user (`DynamicUser`) with no capabilities at all, and
with the rest of the system read-only or hidden (`ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, a system
call filter, and more). `systemd-analyze security penguwatch.service` rates it about 1.2 ("OK"); CI fails if it rises
above 1.5. Data is in `/var/lib/penguwatch`, the config file in `/etc/penguwatch/config.yaml` and the
[settings file](#settings-and-secrets) in `/etc/penguwatch/penguwatch.env` (readable by root only; systemd reads it).

Without sudo: add `--user`. That installs a systemd *user* unit that runs as you (program in
`~/.local/share/penguwatch/app`, data in `~/.local/share/penguwatch`, config and settings in `~/.config/penguwatch`).
A user service stops when you log out unless lingering is on: `sudo loginctl enable-linger $USER`.

## Raspberry Pi

**Needs:** a **64-bit** Raspberry Pi OS (Pi 3, 4, 5 and Zero 2 W). The 32-bit images are not supported: several
dependencies publish no 32-bit ARM wheels, and the scripts refuse to run there (`uname -m` says `aarch64` on a 64-bit
system). Then follow [Linux](#linux): the system service is the right choice on a Pi that stays on, and a Pi that you
only reach over SSH needs [PenguWatch opened to other devices](#opening-penguwatch-to-other-devices).

## Container

One deployment option among the others, and the right one for a NAS or a server that already runs containers. The image
is `ghcr.io/pengupowered/penguwatch` for Intel/AMD (amd64) and ARM (arm64, including a Raspberry Pi 4 or newer with a
64-bit system). Tags are exact versions (`0.16.0`) and `latest`. It is built from the same wheel as the native install,
runs as an unprivileged user (uid 10001) with a read-only root, and writes only to `/app/data` (a volume) and `/tmp`.

### Compose, from the published image

```bash
mkdir penguwatch && cd penguwatch
curl -fsSL https://raw.githubusercontent.com/pengupowered/penguwatch/main/compose.release.yaml -o compose.yaml
curl -fsSL https://raw.githubusercontent.com/pengupowered/penguwatch/main/config.example.yaml -o config.yaml
curl -fsSL https://raw.githubusercontent.com/pengupowered/penguwatch/main/.env.example -o .env    # optional
docker compose up -d              # or: podman compose up -d
```

`config.yaml` only needs to exist (compose mounts it); the defaults suit most installs, and it is read for the listen
address and the database location. Household names, services, prices, notifications and everything else are set in the
app, on the Settings and Services & Sync tabs. In `.env`, set `PENGUWATCH_ACCESS_TOKEN` if the port is reachable by
anyone you do not trust. Open `http://<host-ip>:8080`.

The compose file publishes port 8080 on all interfaces; to keep it on the host only, change the port line to
`"127.0.0.1:8080:8080"`.

### Compose, from a clone (builds the image)

```bash
git clone https://github.com/pengupowered/penguwatch.git && cd penguwatch
cp config.example.yaml config.yaml
cp .env.example .env              # optional
docker compose up -d --build      # or: podman-compose up -d --build
```

### Without compose

```bash
docker run -d --name penguwatch --restart unless-stopped -p 8080:8080 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -v penguwatch_data:/app/data -v "$PWD/config.yaml:/app/config.yaml:ro" \
  ghcr.io/pengupowered/penguwatch:0.16.0
```

In the container the config is `/app/config.yaml` and the data is `/app/data`, which also holds `secrets.json` (the
digest and webhook addresses and signing secrets you enter on the Settings tab). An existing root-owned data volume needs
a one-time `chown -R 10001:0` on its contents.

### Upgrading the container

Change the tag in `compose.yaml` to the new version and run `docker compose up -d` again (from a clone: `git pull`,
then `docker compose up -d --build`). Back up first, and back up `secrets.json` with the database
([Operations](operations.md#backups)).

Upgrading from 0.15: take the new `compose.release.yaml` (or, if you keep your own copy, change the lines for
`PENGUWATCH_LOG_LEVEL`, `PENGUWATCH_NOTIFY_KIND` and `PENGUWATCH_NOTIFY_HOUR` to `${NAME:-}`; the old defaults would pin
those three settings so the Settings tab could not change them). On the first start the app reads your `config.yaml`
once and stores its settings; see [Configuration](configuration.md#the-one-time-import).

## First run

1. Open `http://127.0.0.1:8080` (on the same computer; the install script opens it for you).
2. Choose where the library lives: the library service, SIMKL by default (create an app at [simkl.com/settings/developer](https://simkl.com/settings/developer/app/new),
   enter its client ID under **Services & Sync** and connect with the Device PIN) or no account at all (local library
   and TVmaze, see the [README](../README.md#without-an-account)). The first sync imports your library from the account;
   review mode (on by default) means nothing is sent to the library service until you approve it.
3. Mark the services you subscribe to this month and check their prices.

The full walk-through is [Getting started](getting-started.md).

## Settings and secrets

`config.yaml` holds only where PenguWatch listens and where its database is; everything else is on the Settings tab
([Configuration](configuration.md)), and the digest and webhook addresses and signing secrets you enter there are kept
in `secrets.json` in the data folder. Deployment settings (the access token, and optionally pins for the digest address,
time zone and the like) are environment variables. A native install keeps them in **`penguwatch.env`**, next to `config.yaml`
(`penguwatch paths` prints where). The installer creates it with only comments in it and readable by its owner only; it
is never replaced by an upgrade.

```
# penguwatch.env: one NAME=value per line, no quotes needed, lines starting with # are comments
PENGUWATCH_ACCESS_TOKEN=a-long-random-string
PENGUWATCH_NOTIFY_URL=https://ntfy.example/penguwatch
```

Generate a token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Restart the service after
changing the file (`sudo systemctl restart penguwatch`, `systemctl --user restart penguwatch`,
`sudo launchctl kickstart -k system/com.pengupowered.penguwatch`, or on Windows end the task and run it again with
`schtasks /End /TN PenguWatch` and `schtasks /Run /TN PenguWatch`). A variable that is already set in the environment
wins over the file. PenguWatch loads the file itself whatever started it (a terminal, systemd, launchd or the logon
task), so no secret is ever written into a service definition, and nothing prints its content. Keep it out of backups
you share and out of version control. The container uses `.env` instead.

## Opening PenguWatch to other devices

By default PenguWatch listens on `127.0.0.1`, this computer only, which is also why no firewall prompt appears. To open
it from a phone or another computer (the usual case on a Raspberry Pi or a server):

1. Put a long random `PENGUWATCH_ACCESS_TOKEN` in the [settings file](#settings-and-secrets). Without a token, anyone who
   can reach the port can read your library and change your settings.
2. Listen on the network: install with `--host 0.0.0.0` (`-ListenHost 0.0.0.0` on Windows), or run
   `penguwatch service install --host 0.0.0.0` (add `--system` or `--user` as the same install used), or set
   `server.host: "0.0.0.0"` in `config.yaml` and restart. The install keeps a `--host` or `--port` you gave earlier when
   you run it again for an upgrade.
3. Open `http://<computer's address>:8080` and sign in with the token. A name that is not an IP address, a single word
   or `.lan`, `.local`, `.home.arpa` or `.internal` must be added to `PENGUWATCH_ALLOWED_HOSTS` ([Security](security.md)).
4. Your computer's firewall must allow the port; the install scripts do not open it.

The default for new installs is one constant, `DEFAULT_HOST` in `src/penguwatch/paths.py`.

## Upgrade

Run the same install script again (it fetches the newest release, or pass `--version X.Y.Z`). It first runs
`penguwatch backup` (a consistent copy of the database and the sessions file in `backups/` inside the data folder; for a
system service it runs as the service's own account, so it can read the data: on Linux through `systemd-run` with the
service's DynamicUser identity, on macOS with `sudo -u _penguwatch`) and stops if the backup fails, stops the old
service, reinstalls the program from the new release and installs the service again with the same `--host` and
`--port`. Your settings and data are not touched; database migrations run on the next start. Containers: see above, and
for the program installed with uv, [the uv section](#uv-tool-install-or-pipx).

## Backing up the data

`penguwatch backup [--to DIR]` copies the database (a consistent snapshot, safe while PenguWatch runs) and the sessions
file to a timestamped folder (default `backups/` next to the database). The copy contains your account login: keep it
private. For a **system service** the data belongs to the service's own account, so the plain command, run as you,
looks in your home folder and finds nothing. Use `--system`:
`sudo /opt/penguwatch/venv/bin/penguwatch backup --system --to ~/penguwatch-backups` (`--to` is required), and
`sudo /opt/penguwatch/venv/bin/penguwatch paths --system` shows where the service keeps its files. In the container:
`docker exec penguwatch penguwatch backup` (then copy the folder out). The app can also serve a copy without the account
login: `GET /api/export/backup.db`.

To restore, stop PenguWatch, put the file in place as `penguwatch.db` (delete any `penguwatch.db-wal` and
`penguwatch.db-shm` next to it) and start it again. A backup made by the export has no account login: reconnect the library service.

## Uninstall

```sh
curl -fsSL https://github.com/pengupowered/penguwatch/releases/latest/download/install.sh | sh -s -- --uninstall
```

On Windows: `install.ps1 -Uninstall`. This removes the service and the program (installed with uv instead? see
[the uv section](#uv-tool-install-or-pipx)); **your data stays** (the database, the config and the settings file;
`penguwatch paths` prints where). Delete those yourself if you want them gone. On macOS the hidden `_penguwatch` account
also stays, because it owns the data. Without the script: `penguwatch service uninstall` removes only the service.

If you set the access token with `systemctl edit penguwatch` instead of the settings file, that file
(`/etc/systemd/system/penguwatch.service.d/`) holds the token; the uninstall removes it.

## The `penguwatch` commands

- `penguwatch service install [--system | --user] [--host H] [--port N] [--dry-run]`: writes the unit, plist or task for
  this system and starts it. As root the default is the system service, otherwise the per-user one; Windows has only the
  per-user task. `--dry-run` prints what it would do and the files, changing nothing.
- `penguwatch service uninstall [--system | --user]`: stops and removes the service, keeping the data.
- `penguwatch service status`: whether the service is installed, running and answering `/api/health`. Exit 0 yes, 1
  installed but not answering, 3 not installed.
- `penguwatch` (or `penguwatch serve`) starts the server in this terminal, `penguwatch paths` shows where the config,
  settings file, data and database are and the listen address, `penguwatch init-config` writes an example `config.yaml` (optional),
  `penguwatch backup` copies the data, and `penguwatch doctor` checks that the port is free (or served by PenguWatch),
  the config file is readable, the data folder is writable, the time zone is known and each driver's service is
  reachable (`--offline` skips that last check; exit 0 unless something is wrong). `penguwatchw` is the same program without a
  console window (Windows).

## Troubleshooting

First run `penguwatch doctor` and `penguwatch service status`. The script prints where `penguwatch` is; if the short
command is not found, give its full path.

**Linux**

- Log: `journalctl -u penguwatch -e` (system service) or `journalctl --user -u penguwatch -e` (per-user).
- A per-user service that stops when you log out: `sudo loginctl enable-linger $USER`.
- `systemd-analyze security penguwatch.service` shows how locked down the system service is.
- Port in use: install with `--port N` or stop the other program. Ports below 1024 are not possible (the unit has no
  capabilities).

**macOS**

- Log: `penguwatch.log` in the data folder (`/Library/Application Support/PenguWatch` for the daemon,
  `~/Library/Logs/PenguWatch` for `--user`). `sudo launchctl print system/com.pengupowered.penguwatch` (or
  `launchctl print gui/$(id -u)/com.pengupowered.penguwatch` for the agent) shows its state.

**Windows**

- Log: `%LOCALAPPDATA%\PenguWatch\penguwatch.log`. The task is `PenguWatch` in Task Scheduler
  (`schtasks /query /tn PenguWatch`).
- It runs only while you are signed in. If `irm ... | iex` is blocked by your execution policy, download `install.ps1`
  and run `powershell -ExecutionPolicy Bypass -File install.ps1`.
- Security software that blocks the script: it only downloads from github.com and the uv release host, and the file
  hashes are checked ([Checking a download](#checking-a-download)).

**Everywhere**

- `http://127.0.0.1:8080/api/health` answers 200 once PenguWatch is up.
- A wrong time on "today" and renewal dates: set `PENGUWATCH_TZ` or `server.timezone` (for example `Europe/Berlin`);
  `penguwatch doctor` says whether the name is known.

## Checking a download

Each release lists `SHA256SUMS` (it covers the wheel, the sdist, the lock and both install scripts) and the scripts check
it themselves. The files also carry signed build provenance, which proves they were built from this repository by the
release workflow (needs the [GitHub CLI](https://cli.github.com/)):

```sh
gh attestation verify penguwatch-X.Y.Z-py3-none-any.whl --repo pengupowered/penguwatch
```

The container image has one too: `gh attestation verify oci://ghcr.io/pengupowered/penguwatch:X.Y.Z --repo pengupowered/penguwatch`.

What the `SHA256SUMS` check protects against: a corrupt or partial download, a file from another release mixed in, and
files swapped in a `--from` folder. What it does not: a compromised release, which replaces the files and `SHA256SUMS`
together, because both come from the same place. The download is protected by TLS, so never point `PENGUWATCH_RELEASE_URL`
at an `http://` address. The attestation above (optional) is what proves who built the files.

## uv tool install or pipx

If you already have [uv](https://docs.astral.sh/uv/) (or pipx), this installs the same wheel from PyPI into its own
environment and puts `penguwatch` on your PATH, with Python 3.11 or newer:

```bash
uv tool install penguwatch        # or: pipx install penguwatch
penguwatch init-config            # optional: writes an example config.yaml (listen address and database location)
penguwatch                        # http://127.0.0.1:8080
```

It does **not** check dependency hashes (use the install script for that). Options: `--host`, `--port`, `--config FILE`,
`--data-dir DIR`. To keep it running in the background, run `penguwatch service install --user`. Config and data live in
your per-user folders:

| | Linux | macOS | Windows |
|---|---|---|---|
| config file and `penguwatch.env` | `~/.config/penguwatch/` | `~/Library/Application Support/PenguWatch/` | `%APPDATA%\PenguWatch\` |
| data (database, sessions, log) | `~/.local/share/penguwatch/` | `~/Library/Application Support/PenguWatch/` | `%LOCALAPPDATA%\PenguWatch\` |

On Linux, `XDG_CONFIG_HOME` and `XDG_DATA_HOME` move them. The data folder is created with owner-only permissions.
Override anything with `--config`, `--data-dir` or the variables `PENGUWATCH_CONFIG`, `PENGUWATCH_DATA`,
`PENGUWATCH_DB_PATH` and `PENGUWATCH_ENV_FILE`; an option wins over a variable, a variable over the config file.

**Upgrade** with uv: back up first (`penguwatch backup`), then `uv tool upgrade penguwatch`, then
`penguwatch service install --user`. That restarts the service on the new program and keeps its listen address and port.
Until you do, the service keeps running the old program.

**Remove** in this order: `penguwatch service uninstall --user` first, then `uv tool uninstall penguwatch`. If you
remove the program first, the service stays registered but no longer starts, because the program it starts is gone (if
you did, run `systemctl --user disable --now penguwatch` and delete `~/.config/systemd/user/penguwatch.service`). Your
data stays either way.
