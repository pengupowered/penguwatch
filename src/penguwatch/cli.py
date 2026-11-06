"""The `penguwatch` command (installed by `uv tool install penguwatch` or `pipx install penguwatch`).

    penguwatch [serve] [--host H] [--port N] [--config FILE] [--data-dir DIR]
                                   start the server (the default)
    penguwatch init-config [--force]
                                   write the example config.yaml to your config folder
    penguwatch paths [--system]    config file, data folder, database and listen address, and where each came from
    penguwatch backup [--to DIR] [--system]
                                   a consistent copy of the database (safe while the app runs) and the sessions file,
                                   in a timestamped folder (default: <data folder>/backups)
    penguwatch doctor [--offline]  can it start here? Port, config file, data folder, services, time zone
    penguwatch service install|uninstall|status
                                   start PenguWatch automatically (systemd, launchd or a logon task; penguwatch.service)
    penguwatch --version
    penguwatchw                    the same command without a console window (Windows; what the logon task starts)

Files live in per-user folders (penguwatch.paths has the rules). The command first loads penguwatch.env next to the
config file (penguwatch.envfile), then sets the PENGUWATCH_* variables the app already reads and starts it, so an
environment variable you set yourself still wins over the config file.
Importing this module loads no web framework; only `serve` does.
"""

import argparse
import errno
import os
import shlex
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import yaml

from penguwatch import __version__, envfile, paths, service

LOG_ROTATE_BYTES = 5_000_000
EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.yaml"


def _read_config(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"penguwatch: {path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"penguwatch: {path} must be a mapping of settings")
    return data


def _section(data: dict, name: str) -> dict:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def resolve(args: argparse.Namespace) -> dict:
    """The settings `serve` applies, as {name: (value, where it came from)}; sets nothing."""
    env = os.environ
    option = {name: getattr(args, name, None) for name in ("data_dir", "config", "host", "port")}
    out = {}
    data_value = option["data_dir"] or env.get("PENGUWATCH_DATA")
    out["data"] = (str(Path(data_value).expanduser()), "--data-dir" if option["data_dir"] else "PENGUWATCH_DATA") \
        if data_value else (str(paths.default_data_dir()), "default for this system")

    config_value = option["config"] or env.get("PENGUWATCH_CONFIG")
    if config_value:
        config = Path(config_value).expanduser()
        out["config"] = (str(config), "--config" if option["config"] else "PENGUWATCH_CONFIG")
    else:
        config = paths.config_path()
        out["config"] = (str(config), "default for this system")
    exists = config.is_file()
    out["config_exists"] = (exists, "")
    raw = _read_config(config) if exists else {}
    server, database = _section(raw, "server"), _section(raw, "database")

    if option["host"]:
        out["host"] = (option["host"], "--host")
    elif env.get("PENGUWATCH_HOST"):
        out["host"] = (env["PENGUWATCH_HOST"], "PENGUWATCH_HOST")
    elif server.get("host"):
        out["host"] = (str(server["host"]), "server.host in the config")
    else:
        out["host"] = (paths.DEFAULT_HOST, "default")
    if option["port"]:
        out["port"] = (option["port"], "--port")
    elif env.get("PENGUWATCH_PORT"):
        out["port"] = (env["PENGUWATCH_PORT"], "PENGUWATCH_PORT")
    elif server.get("port"):
        out["port"] = (server["port"], "server.port in the config")
    else:
        out["port"] = (paths.DEFAULT_PORT, "default")

    # the data folder as it will be (--data-dir wins over the variable) decides the database
    base = Path(out["data"][0])
    if env.get("PENGUWATCH_DB_PATH"):
        out["db"] = (env["PENGUWATCH_DB_PATH"], "PENGUWATCH_DB_PATH")
    elif database.get("db_path") and database["db_path"] != paths.LEGACY_DB_PATH:
        out["db"] = (str(database["db_path"]), "database.db_path in the config")
    else:
        out["db"] = (str(base / paths.DB_FILE), "<data folder>/" + paths.DB_FILE)
    return out


def _quiet_streams(folder: Path) -> None:
    """`penguwatchw` (Windows, no console) has no stdout or stderr, and the logging handler and uvicorn write to them.
    They go to <data folder>/penguwatch.log (the previous one is kept as penguwatch.log.1 once it passes 5 MB), or
    nowhere if that file can't be opened."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = folder / paths.LOG_FILE
        if log.exists() and log.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(log, folder / (paths.LOG_FILE + ".1"))
        stream = open(log, "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, stream)


def _load_env_file(args: argparse.Namespace) -> None:
    """penguwatch.env next to the config file (the --config option counts) into the environment; never prints a value."""
    env = dict(os.environ)
    if getattr(args, "config", None):
        env["PENGUWATCH_CONFIG"] = args.config
    for problem in envfile.load(paths.env_file_path(env)):
        print(f"penguwatch: warning: {problem}", file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> int:
    _load_env_file(args)
    settings = resolve(args)
    _quiet_streams(Path(settings["data"][0]))
    config = settings["config"][0]
    if not settings["config_exists"][0]:
        if args.config or os.environ.get("PENGUWATCH_CONFIG"):
            print(f"penguwatch: warning: {config} does not exist; using the defaults, with no streaming services",
                  file=sys.stderr)
        else:
            print(f"penguwatch: no config file at {config}; using the built-in example. "
                  "`penguwatch init-config` writes one you can edit.", file=sys.stderr)
            config = str(EXAMPLE_CONFIG)
    os.environ["PENGUWATCH_CONFIG"] = config
    os.environ["PENGUWATCH_DATA"] = settings["data"][0]
    os.environ["PENGUWATCH_DB_PATH"] = settings["db"][0]
    os.environ["PENGUWATCH_HOST"] = str(settings["host"][0])
    os.environ["PENGUWATCH_PORT"] = str(settings["port"][0])
    try:
        Path(settings["db"][0]).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        print(f"penguwatch: cannot create the data folder {Path(settings['db'][0]).parent}: {exc}", file=sys.stderr)
        return 1
    from penguwatch import main as app_main  # imported late: it builds the whole app

    app_main.main()
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    target = Path(args.config).expanduser() if args.config else paths.config_path()
    if target.exists() and not args.force:
        print(f"penguwatch: {target} already exists (--force replaces it)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    print(target)
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    _use_system_service(args)
    _load_env_file(args)
    settings = resolve(args)
    config, source = settings["config"]
    rows = [
        ("config file", f"{config}{'' if settings['config_exists'][0] else '  (missing: run `penguwatch init-config`)'}",
         source),
        ("data folder", *settings["data"]),
        ("database", *settings["db"]),
        ("listen host", *settings["host"]),
        ("listen port", str(settings["port"][0]), settings["port"][1]),
        ("settings file", str(paths.env_file_path()), "penguwatch.env next to the config file"),
    ]
    width = max(len(r[0]) for r in rows)
    for name, value, where in rows:
        print(f"{name:<{width}}  {value}   [{where}]")
    return 0


def _use_system_service(args: argparse.Namespace) -> bool:
    """With --system: look where the system service keeps its files (and listens), not in this account's own place.
    Done through the environment, which every lookup reads first. Returns whether it did."""
    if not getattr(args, "system", False):
        return False
    kind = service.system_kind()
    data, config = service.system_locations(kind)
    os.environ["PENGUWATCH_DATA"], os.environ["PENGUWATCH_CONFIG"] = data, config
    os.environ.pop("PENGUWATCH_DB_PATH", None)
    for key, value in service.existing_settings(kind, service.SYSTEM).items():
        os.environ.setdefault(key, value)
    return True


def _needs_sudo(command: str) -> SystemExit:
    """What a normal user sees instead of a traceback when the system service's private files can't be read."""
    exe = sys.argv[0] if os.path.isabs(sys.argv[0]) else (shutil.which("penguwatch") or "penguwatch")
    return SystemExit("The system service's files belong to its own account: run this with sudo.\n"
                      f"  sudo {exe} {command}")


def _copy_private_file(source: Path, target: Path) -> None:
    """Copies a regular file without following a symbolic link (a link in the data folder could point at any file this
    account can read). The source is opened with O_NOFOLLOW where the OS has it; a link is refused everywhere."""
    if source.is_symlink():
        raise OSError(f"{source} is a symbolic link; refusing to back up what it points to")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) == getattr(errno, "ELOOP", None):
            raise OSError(f"{source} is a symbolic link; refusing to back up what it points to") from exc
        raise
    with os.fdopen(fd, "rb") as src, open(target, "xb") as dst:
        shutil.copyfileobj(src, dst)


def make_backup(db: Path, to: Optional[str] = None, now: Optional[datetime] = None) -> Path:
    """A timestamped folder with a consistent copy of the database (VACUUM INTO on a read-only connection: safe while
    the app writes, WAL included) and the sessions file next to it. The copy holds the account logins: keep it private."""
    if not db.is_file():
        raise SystemExit(f"penguwatch backup: no database at {db} (nothing to back up)")
    parent = Path(to).expanduser() if to else paths.backups_dir(str(db))
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    folder = parent / stamp
    if folder.exists():
        raise SystemExit(f"penguwatch backup: {folder} already exists; try again in a second")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700)
    try:
        con = sqlite3.connect(f"file:{quote(str(db.resolve()))}?mode=ro", uri=True, timeout=30)
        try:
            con.execute("VACUUM INTO ?", (str(folder / paths.DB_FILE),))
        finally:
            con.close()
        os.chmod(folder / paths.DB_FILE, 0o600)
        sessions = paths.sessions_path(str(db))
        if sessions.is_symlink() or sessions.is_file():
            target = folder / sessions.name
            _copy_private_file(sessions, target)
            os.chmod(target, 0o600)
    except (sqlite3.Error, OSError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise SystemExit(f"penguwatch backup failed: {exc}") from exc
    return folder


def cmd_backup(args: argparse.Namespace) -> int:
    system = _use_system_service(args)
    if system and not args.to:
        raise SystemExit("penguwatch backup --system: say where the copy goes, e.g. "
                         "sudo penguwatch backup --system --to ~/penguwatch-backups")
    if system and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise _needs_sudo(f"backup --system --to {shlex.quote(args.to)}")
    _load_env_file(args)
    db = Path(resolve(args)["db"][0])
    try:
        print(make_backup(db, args.to))
    except PermissionError:
        if not system:
            raise
        raise _needs_sudo("backup --system --to " + shlex.quote(args.to)) from None
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from penguwatch import doctor

    _load_env_file(args)
    return doctor.run(resolve(args), offline=args.offline)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="penguwatch", description="TV and movie tracker with a streaming-subscription "
                                     "planner and two-way sync with your tracker. No command: start the server.")
    parser.add_argument("--version", action="version", version=f"penguwatch {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{serve,init-config,paths,backup,doctor,service}")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", metavar="FILE", help="config file (same as PENGUWATCH_CONFIG)")
        p.add_argument("--data-dir", metavar="DIR", help="data folder for the database (same as PENGUWATCH_DATA)")

    for name, func, help_text in (("serve", cmd_serve, "start the server (the default)"),
                                  ("paths", cmd_paths, "show where the config, data and database are")):
        p = sub.add_parser(name, help=help_text)
        common(p)
        p.add_argument("--host", help="listen address (same as PENGUWATCH_HOST)")
        p.add_argument("--port", type=int, help="listen port (same as PENGUWATCH_PORT)")
        p.set_defaults(func=func)
    sub.choices["paths"].add_argument("--system", action="store_true",
                                      help="the system service's files (/var/lib/penguwatch on Linux)")
    backup = sub.add_parser("backup", help="copy the database and the sessions file into a timestamped folder")
    common(backup)
    backup.add_argument("--to", metavar="DIR", help="parent folder for the timestamped backup (default: <data>/backups)")
    backup.add_argument("--system", action="store_true",
                        help="back up the system service's data (run with sudo; needs --to)")
    backup.set_defaults(func=cmd_backup)
    doctor = sub.add_parser("doctor", help="check the port, config file, data folder, the drivers' services and time zone")
    common(doctor)
    doctor.add_argument("--host", help=argparse.SUPPRESS)
    doctor.add_argument("--port", type=int, help=argparse.SUPPRESS)
    doctor.add_argument("--offline", action="store_true", help="skip the checks that need the internet (the drivers' services)")
    doctor.set_defaults(func=cmd_doctor)
    service.add_parser(sub)
    init = sub.add_parser("init-config", help="write the example config.yaml to your config folder")
    init.add_argument("--config", metavar="FILE", help="write here instead of the default place")
    init.add_argument("--force", action="store_true", help="replace an existing file")
    init.set_defaults(func=cmd_init_config)
    # `penguwatch --port 9000` is `penguwatch serve --port 9000`
    parser.add_argument("--host", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    common(parser)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    func = getattr(args, "func", cmd_serve)
    try:
        return func(args)
    except service.ServiceError as exc:
        print(f"penguwatch: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # a bad PENGUWATCH_PORT or config file: one line, not a traceback
        print(f"penguwatch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
