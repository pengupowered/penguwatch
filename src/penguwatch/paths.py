"""Where PenguWatch keeps its files when it is started with the `penguwatch` command.

Data directory (the database, and next to it the session file and secrets.json):
    PENGUWATCH_DATA, else the system's per-user location: $XDG_DATA_HOME/penguwatch or ~/.local/share/penguwatch
    (Linux and others), ~/Library/Application Support/PenguWatch (macOS), %LOCALAPPDATA%\\PenguWatch (Windows).
Config file:
    PENGUWATCH_CONFIG, else config.yaml in the per-user config folder: $XDG_CONFIG_HOME/penguwatch or
    ~/.config/penguwatch (Linux and others), the same folder as the data on macOS, %APPDATA%\\PenguWatch (Windows).
Database:
    PENGUWATCH_DB_PATH, else `database.db_path` in the config, else <data directory>/penguwatch.db.

The container sets PENGUWATCH_DATA=/app/data and PENGUWATCH_CONFIG=/app/config.yaml. Starting the app with
`python -m penguwatch.main` (a source checkout) does not use this module: it reads config.yaml from the working
directory and keeps data/penguwatch.db there, as it always did.

Settings for the background service (an access token, a notification URL, ...) go in penguwatch.env next to the config
file: see penguwatch.envfile.

Nothing here creates a directory except ensure_data_dir().
"""

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

DB_FILE = "penguwatch.db"
CONFIG_FILE = "config.yaml"
ENV_FILE = "penguwatch.env"
LOG_FILE = "penguwatch.log"
SESSIONS_FILE = "sessions.json"  # next to the database (penguwatch.security)
SECRETS_FILE = "secrets.json"  # next to the database: the Settings tab's write-only secrets (penguwatch.secrets)
BACKUPS_DIR = "backups"
# THE default listen address of a native install (the `penguwatch` command, and the background service the installers
# set up). 127.0.0.1 is this computer only; "0.0.0.0" would be every network connection, which needs an access token
# (docs/security.md). The container and your config say otherwise. Everything that names the default reads these two.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
# The value of `database.db_path` before this module existed (and of the old example config): relative to the working
# directory. The command treats it as "not set", so an old config does not pin the database to wherever you start from.
LEGACY_DB_PATH = "data/penguwatch.db"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _xdg(env: Mapping[str, str], name: str, fallback: Path) -> Path:
    value = env.get(name)
    return Path(value) if value and Path(value).is_absolute() else fallback


def default_data_dir(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    env, platform = _env(env), sys.platform if platform is None else platform
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "PenguWatch"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PenguWatch"
    return _xdg(env, "XDG_DATA_HOME", Path.home() / ".local" / "share") / "penguwatch"


def default_config_dir(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    env, platform = _env(env), sys.platform if platform is None else platform
    if platform == "win32":
        base = env.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "PenguWatch"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PenguWatch"
    return _xdg(env, "XDG_CONFIG_HOME", Path.home() / ".config") / "penguwatch"


def data_dir(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    configured = _env(env).get("PENGUWATCH_DATA")
    return Path(configured).expanduser() if configured else default_data_dir(env, platform)


def data_dir_source(env: Optional[Mapping[str, str]] = None) -> str:
    return "PENGUWATCH_DATA" if _env(env).get("PENGUWATCH_DATA") else "default for this system"


def ensure_data_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """Creates the data directory if it is missing (mode 0700 where the system has modes) and returns it."""
    path = data_dir(env)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def config_path(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    configured = _env(env).get("PENGUWATCH_CONFIG")
    return Path(configured).expanduser() if configured else default_config_dir(env, platform) / CONFIG_FILE


def config_source(env: Optional[Mapping[str, str]] = None) -> str:
    return "PENGUWATCH_CONFIG" if _env(env).get("PENGUWATCH_CONFIG") else "default for this system"


def db_path(configured: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    """The database file: PENGUWATCH_DB_PATH, else the configured path (unless it is the legacy relative default),
    else <data directory>/penguwatch.db."""
    from_env = _env(env).get("PENGUWATCH_DB_PATH")
    if from_env:
        return from_env
    if configured and configured != LEGACY_DB_PATH:
        return str(configured)
    return str(data_dir(env) / DB_FILE)


def env_file_path(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """The file of environment variables (KEY=value lines) loaded at start: PENGUWATCH_ENV_FILE, else penguwatch.env
    next to the config file."""
    configured = _env(env).get("PENGUWATCH_ENV_FILE")
    return Path(configured).expanduser() if configured else config_path(env, platform).parent / ENV_FILE


def sessions_path(db: str) -> Path:
    """Where sign-outs are kept: sessions.json next to the database file."""
    return Path(db).parent / SESSIONS_FILE


def secrets_path(db: str) -> Path:
    """Where the Settings tab's secrets are kept (the digest address, the webhooks' addresses and signing secrets):
    secrets.json next to the database file, mode 0600. Back it up with the database."""
    return Path(db).parent / SECRETS_FILE


def backups_dir(db: str) -> Path:
    return Path(db).parent / BACKUPS_DIR


def listen_address(server: Optional[dict] = None, env: Optional[Mapping[str, str]] = None):
    """(host, port, where the host came from, where the port came from): the variables, then the config's `server`
    section (a dict, or None), then the defaults. An unusable PENGUWATCH_PORT raises ValueError."""
    env, server = _env(env), server or {}
    host, host_from = DEFAULT_HOST, "default"
    if env.get("PENGUWATCH_HOST"):
        host, host_from = env["PENGUWATCH_HOST"], "PENGUWATCH_HOST"
    elif server.get("host"):
        host, host_from = str(server["host"]), "server.host in the config"
    port, port_from = DEFAULT_PORT, "default"
    if env.get("PENGUWATCH_PORT"):
        try:
            port, port_from = int(env["PENGUWATCH_PORT"]), "PENGUWATCH_PORT"
        except ValueError:
            raise ValueError(f"PENGUWATCH_PORT must be a number, not {env['PENGUWATCH_PORT']!r}") from None
    elif server.get("port"):
        port, port_from = int(server["port"]), "server.port in the config"
    return host, port, host_from, port_from
