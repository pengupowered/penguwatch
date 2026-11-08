"""`penguwatch doctor`: can PenguWatch start here, and will it work once it has?

Checks, each one line `ok`, `warn` or `FAIL`:
    config file    exists and is valid YAML with settings in it (a missing file is a warning: the built-in example is used)
    data folder    exists and can be written to, or can be created
    port           free, or already served by PenguWatch (/api/health), or taken by something else (FAIL)
    time zone      the household zone (PENGUWATCH_TZ, the stored setting, else server.timezone in the config; TZ) is one
                   the system knows
    settings       where the settings are (the revision, the import date) and how many config.yaml keys differ from
                   them (by name); a pinning variable that is set but not valid (ignored) is a warning
    <service>      each driver's service answers over HTTPS (a warning when it does not: the app works without it)
Exit status 0 unless a check says FAIL. Nothing is created or changed (the database is opened read-only), and no
setting's value is printed.
"""

import json
import socket
import sqlite3
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

OK, WARN, FAIL = "ok", "warn", "FAIL"
Row = Tuple[str, str, str]  # (level, what, detail)



def check_config(path: Path, exists: bool) -> Row:
    import yaml

    if not exists:
        return WARN, "config file", f"{path} does not exist; the built-in example is used (`penguwatch init-config` writes one)"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        return FAIL, "config file", f"cannot read {path}: {exc.strerror or exc.__class__.__name__}"
    except yaml.YAMLError as exc:
        return FAIL, "config file", f"{path} is not valid YAML: {str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__}"
    except UnicodeDecodeError:
        return FAIL, "config file", f"{path} is not UTF-8 text"
    if not isinstance(data, dict):
        return FAIL, "config file", f"{path} must be a mapping of settings"
    return OK, "config file", f"{path} is readable ({len(data)} sections)"


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def check_data_dir(folder: Path) -> Row:
    import os

    if folder.is_dir():
        if os.access(folder, os.W_OK | os.X_OK):
            return OK, "data folder", f"{folder} is writable"
        return FAIL, "data folder", f"{folder} is not writable by this account"
    parent = _nearest_existing(folder.parent)
    if parent.is_dir() and os.access(parent, os.W_OK | os.X_OK):
        return OK, "data folder", f"{folder} does not exist yet; it will be created"
    return FAIL, "data folder", f"{folder} does not exist and {parent} is not writable, so it cannot be created"


def check_port(host: str, port: int, health: Callable[[str, int], Optional[dict]]) -> Row:
    what = "port"
    if health(host, port):
        return OK, what, f"{port} is served by PenguWatch already (it is running)"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as exc:
        return FAIL, what, f"{host}:{port} cannot be used ({exc.strerror or exc.__class__.__name__}); " \
            "something else holds it: pick another with --port"
    return OK, what, f"{host}:{port} is free"


def check_time_zone(configured: Optional[str], env_tz: Optional[str]) -> Row:
    from zoneinfo import ZoneInfo

    name = (configured or "").strip() or (env_tz or "").strip()
    if not name:
        return OK, "time zone", "none set: days follow UTC (set PENGUWATCH_TZ or server.timezone, e.g. Europe/Berlin)"
    if name.upper() == "UTC":
        return OK, "time zone", "UTC"
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name, malformed key or missing tzdata all mean the same to the user
        return FAIL, "time zone", f"{name!r} is not a time zone this system knows (use a name like Europe/Berlin; " \
            "on Windows the tzdata package must be installed)"
    return OK, "time zone", name


def read_settings_state(db: Path) -> Optional[Dict[str, Any]]:
    """The settings store of the database, read-only (never written, no side files when no app has it open):
    {"initialized", "revision", "imported_at", "values"}; None when there is no database or it cannot be read."""
    if not db.is_file():
        return None
    mode = "mode=ro" if db.with_name(db.name + "-wal").exists() else "mode=ro&immutable=1"
    try:
        con = sqlite3.connect(f"file:{quote(str(db.resolve()))}?{mode}", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        meta = dict(con.execute("SELECT key, value FROM app_state WHERE key IN ('settings_initialized', "
                                "'settings_revision', 'settings_imported_at')").fetchall())
        values = {}
        if meta.get("settings_initialized") == "1":
            for key, raw in con.execute("SELECT key, value FROM settings_values").fetchall():
                try:
                    values[key] = json.loads(raw)
                except ValueError:
                    pass
    except sqlite3.Error:
        return None
    finally:
        con.close()
    try:
        imported_at = float(meta["settings_imported_at"]) if meta.get("settings_imported_at") else None
    except ValueError:
        imported_at = None
    revision = meta.get("settings_revision") or "0"
    return {"initialized": meta.get("settings_initialized") == "1", "revision": int(revision) if revision.isdigit()
            else 0, "imported_at": imported_at, "values": values}


def check_settings(state: Optional[Dict[str, Any]], raw: dict) -> Row:
    """Where the settings are, and the config.yaml keys that differ from them (names only)."""
    from datetime import datetime, timezone

    if state is None or not state["initialized"]:
        return OK, "settings", "not imported yet: the first start imports config.yaml into the Settings tab"
    from penguwatch.settings import registry
    from penguwatch.settings.importer import read_settings
    since = datetime.fromtimestamp(state["imported_at"], timezone.utc).strftime("%Y-%m-%d") \
        if state["imported_at"] else "the import"
    differing = sorted(key for key, value in read_settings(raw).document.values.items()
                       if value != state["values"].get(key, registry.get(key).default()))
    detail = f"in the database since {since}, revision {state['revision']}"
    if differing:
        detail += (f"; config.yaml is ignored for them, {len(differing)} key{'s' if len(differing) != 1 else ''} "
                   f"differ: {', '.join(differing)}")
    return OK, "settings", detail


def check_pins(env: Dict[str, str]) -> List[Row]:
    """A warning per variable that pins a setting but is not valid (it is ignored; the reason names it, never its
    value)."""
    from penguwatch.config import EnvOverrides, env_pins
    read = {name: (env.get(info.validation_alias) or "").strip() or None
            for name, info in EnvOverrides.model_fields.items() if isinstance(info.validation_alias, str)}
    overrides = EnvOverrides.model_construct(**read, driver_values={})  # as written: env_pins checks each one
    return [(WARN, "variable", why) for why in env_pins(overrides)[1].values()]


def check_service(label: str, url: str, timeout: float = 5.0) -> Row:
    """Whether a driver's service answers at `url` (its health_url)."""
    request = urllib.request.Request(url, headers={"User-Agent": "penguwatch-doctor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()):
            pass
    except urllib.error.HTTPError:
        pass  # any HTTP answer (even 404) proves the server is reachable
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return WARN, label, f"{url} is not reachable ({reason.__class__.__name__}); sync will wait until it is"
    return OK, label, f"{urlsplit(url).hostname} is reachable"


def check_services(timeout: float = 5.0) -> List[Row]:
    """check_service for every registered driver that has a service to reach."""
    from penguwatch.drivers import DRIVERS
    return [check_service(d.label, d.health_url, timeout) for d in DRIVERS.values() if getattr(d, "health_url", None)]


def run(settings: dict, offline: bool = False, out=None, health: Optional[Callable] = None,
        services: Callable[[], List[Row]] = check_services, env: Optional[dict] = None) -> int:
    """Prints the checks for the settings `cli.resolve` returned; the exit status."""
    import os
    import sys

    from penguwatch import cli, service

    out = out or sys.stdout
    env = os.environ if env is None else env
    health = health or service.health_body
    config = Path(settings["config"][0])
    exists = settings["config_exists"][0]
    raw = cli._read_config(config) if exists and check_config(config, True)[0] == OK else {}
    rows: List[Row] = [check_config(config, exists)]
    rows.append(check_data_dir(Path(settings["db"][0]).parent))
    try:
        port = int(settings["port"][0])
        rows.append(check_port(str(settings["host"][0]), port, health))
    except ValueError:
        rows.append((FAIL, "port", f"{settings['port'][0]!r} is not a port number"))
    state = read_settings_state(Path(settings["db"][0]))
    stored_zone = state["values"].get("server.timezone") if state and state["initialized"] else None
    zone = stored_zone if state and state["initialized"] else cli._section(raw, "server").get("timezone")
    rows.append(check_time_zone(env.get("PENGUWATCH_TZ") or zone, env.get("TZ")))
    rows.append(check_settings(state, raw))
    rows.extend(check_pins(env))
    rows.extend([(OK, "services", "skipped (--offline)")] if offline else services())
    for level, what, detail in rows:
        print(f"{level:>4}  {what:<12} {detail}", file=out)
    failed = [r for r in rows if r[0] == FAIL]
    print(f"{len(failed)} problem(s)." if failed else "Nothing is wrong.", file=out)
    return 1 if failed else 0
