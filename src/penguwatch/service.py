"""`penguwatch service install | uninstall | status`: run PenguWatch in the background, started by the system.

    Linux     a systemd system unit (as root: DynamicUser, no capabilities, ProtectSystem=strict, the venv in
              /opt/penguwatch, data in /var/lib/penguwatch, config in /etc/penguwatch), or a systemd user unit when
              not root (--user)
    macOS     a LaunchDaemon with a dedicated user `_penguwatch` (as root), or a LaunchAgent (--user)
    Windows   a Task Scheduler task for the current user, started at logon, running `penguwatchw` (no console window)

Every operating system's definition is a pure function of its inputs (`render_*`), and `plan_install` /
`plan_uninstall` turn them into a list of steps that `run_plan` carries out (or `--dry-run` prints). Nothing here
needs more than the standard library. Data folders follow penguwatch.paths: the system services set PENGUWATCH_DATA and
PENGUWATCH_CONFIG, the user ones use the per-user defaults.

Settings that are secrets (the access token, the notification URL) are never written into a definition: they go in
penguwatch.env next to the config file, which the program loads itself (penguwatch.envfile) and the system unit also
names as EnvironmentFile. The installer creates that file empty (mode 0600) and nothing here ever prints its content.
"""

import getpass
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from penguwatch import __version__, envfile, paths

NAME = "penguwatch"
UNIT = "penguwatch.service"
LABEL = "com.pengupowered.penguwatch"
TASK = "PenguWatch"
MAC_USER = "_penguwatch"
# Ending the task may leave the python process under the launcher running, which keeps its files open (and the
# folder undeletable); this ends the launcher and everything it started.
TASKKILL = ("taskkill", "/F", "/T", "/IM", "penguwatchw.exe")

# Paths of another system than the one running (a plan for Linux built on Windows) use the pure classes: no
# host-dependent separators.
SYSTEM_UNIT_PATH = PurePosixPath("/etc/systemd/system") / UNIT
SYSTEM_DATA_DIR = "/var/lib/penguwatch"  # StateDirectory=penguwatch
SYSTEM_CONFIG_DIR = "/etc/penguwatch"
LINUX_APP_DIR = "/opt/penguwatch"
MAC_APP_DIR = "/usr/local/penguwatch"
MAC_DAEMON_PLIST = PurePosixPath("/Library/LaunchDaemons") / f"{LABEL}.plist"
MAC_DAEMON_DATA_DIR = "/Library/Application Support/PenguWatch"

# systemd-analyze security --offline scores the system unit below this exposure (a test and CI check it).
MAX_EXPOSURE = 1.5

SYSTEM, USER = "system", "user"
EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.yaml"


class ServiceError(Exception):
    """Something the user has to fix (not root, wrong platform, a tool is missing); printed as one line."""


# ---- definitions: pure functions ------------------------------------------------------------------------------------

_SD_PLAIN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:@+=,-")


def _no_control(value: str, what: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError(f"{what} must not contain a newline or other control character")
    return value


def _sd_word(value: str, *, exec_line: bool = False) -> str:
    """One word of a systemd unit line (systemd.syntax quoting): `%` is a specifier, and in ExecStart `$` starts a
    variable, so both are doubled; a word with anything but plain characters is double-quoted with `\\` and `"`
    escaped, so a space or a quote can never split it or start another setting."""
    _no_control(value, "A value in the service unit")
    out = value.replace("%", "%%")
    if exec_line:
        out = out.replace("$", "$$")
    if all(c in _SD_PLAIN for c in value):
        return out
    return '"' + out.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exec_start(exe: str) -> str:
    return f"ExecStart={_sd_word(exe, exec_line=True)} serve"


def _condition_exists(exe: str) -> str:
    """Start only while the program is there: after `uv tool uninstall` (or deleting the folder) the unit is skipped
    instead of failing with status 203/EXEC every five seconds, forever."""
    _no_control(exe, "The path of penguwatch")
    return "ConditionPathExists=" + exe.replace("%", "%%")


def _environment(env: Dict[str, str]) -> List[str]:
    lines = []
    for key, value in env.items():
        _no_control(key + value, "A value in the service unit")
        lines.append("Environment=" + _sd_word(f"{key}={value}"))  # one quoted assignment: Environment="KEY=a b"
    return lines


def render_systemd_system(exe: str, env: Optional[Dict[str, str]] = None) -> str:
    """The system unit. DynamicUser gives it a throw-away uid, StateDirectory a data folder it alone can read, and it
    has no capability at all (it listens on an unprivileged port and sends nothing raw). The venv lives outside /home
    because ProtectHome hides it. The settings file is read by systemd as root, so it can stay private to root."""
    env = {"PENGUWATCH_DATA": SYSTEM_DATA_DIR, "PENGUWATCH_CONFIG": f"{SYSTEM_CONFIG_DIR}/{paths.CONFIG_FILE}",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    lines = [
        "[Unit]",
        "Description=PenguWatch, TV and movie tracker",
        "Documentation=https://github.com/pengupowered/penguwatch",
        "After=network-online.target",
        "Wants=network-online.target",
        _condition_exists(exe),
        "",
        "[Service]",
        "Type=simple",
        _exec_start(exe),
        *_environment(env),
        f"EnvironmentFile=-{SYSTEM_CONFIG_DIR}/{paths.ENV_FILE}",
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=20",
        "",
        "# Who it runs as, and what it may write",
        "DynamicUser=yes",
        f"StateDirectory={NAME}",
        "StateDirectoryMode=0700",
        "UMask=0077",
        "",
        "# No privilege at all",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "NoNewPrivileges=yes",
        "",
        "# The rest of the system is read-only or out of sight",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "RemoveIPC=yes",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "SystemCallFilter=~@privileged @resources",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_systemd_user(exe: str, env: Optional[Dict[str, str]] = None) -> str:
    """The user unit: runs as you, so the data folder and the settings file are the per-user defaults. No mount or
    user-namespace sandboxing (ProtectSystem, PrivateTmp): on Ubuntu 24.04 and others an unprivileged user manager is
    not allowed to create the namespaces and the unit would not start. What needs none is on."""
    env = {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    lines = [
        "[Unit]",
        "Description=PenguWatch, TV and movie tracker",
        "Documentation=https://github.com/pengupowered/penguwatch",
        "After=network-online.target",
        _condition_exists(exe),
        "",
        "[Service]",
        "Type=simple",
        _exec_start(exe),
        *_environment(env),
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=20",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "SystemCallArchitectures=native",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_launchd(exe: str, env: Dict[str, str], log: str, user: Optional[str] = None) -> str:
    """A launchd property list: starts at load (boot for a daemon, login for an agent) and restarts if it exits."""
    plist: Dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [exe, "serve"],
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **env},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    if user:
        plist["UserName"] = user
        plist["GroupName"] = user
    return plistlib.dumps(plist, sort_keys=False).decode("utf-8")


def render_task_xml(command: str, arguments: str, account: str, working_dir: str) -> str:
    """The Task Scheduler definition: at logon of `account`, no admin rights, no time limit, restarted if it dies,
    also on battery."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>PenguWatch, TV and movie tracker (starts when you sign in)</Description>
    <URI>\\{TASK}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(account)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(account)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# ---- the plan: what install / uninstall do, as data -----------------------------------------------------------------

@dataclass
class Step:
    """One effect. kind: write (path, text, mode), write_new (the same, only when the file does not exist yet; owner
    gets it), mkdir (path, mode, owner), remove (path), run (argv; tolerate = a failure is fine), mac_user (create
    the _penguwatch account if it is missing), chown (path, owner)."""

    kind: str
    path: Optional[str] = None
    text: Optional[str] = None
    mode: int = 0o644
    argv: Sequence[str] = ()
    tolerate: bool = False
    owner: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "write":
            return f"write {self.path} (mode {self.mode:o})"
        if self.kind == "write_new":
            return f"create {self.path} (mode {self.mode:o}) unless it exists"
        if self.kind == "mkdir":
            return f"create directory {self.path} (mode {self.mode:o})"
        if self.kind == "remove":
            return f"remove {self.path}"
        if self.kind == "chown":
            return f"give {self.path} to {self.owner}"
        if self.kind == "mac_user":
            return f"create the account {MAC_USER} (no login, no home) if it does not exist"
        return "run " + " ".join(_quote(a) for a in self.argv) + ("  (a failure is fine)" if self.tolerate else "")


@dataclass
class Plan:
    mode: str                     # "systemd-system", "systemd-user", "launchd-daemon", "launchd-agent", "windows-task"
    steps: List[Step] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # printed after the steps ran
    files: Dict[str, str] = field(default_factory=dict)  # path -> content of the definition, for --dry-run and tests


def _quote(arg: str) -> str:
    return f"'{arg}'" if (not arg or any(c in arg for c in " \t'\"$")) else arg


def _win_arg(value: str) -> str:
    """One command-line argument for the task's Arguments (CommandLineToArgvW rules): quoted when it has anything
    but plain characters, so a space cannot start another argument."""
    _no_control(value, "A value in the service definition")
    if value and all(c in _SD_PLAIN or c == ":" for c in value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def _env_for(host: Optional[str], port: Optional[int]) -> Dict[str, str]:
    env = {}
    if host:
        env["PENGUWATCH_HOST"] = _no_control(host, "--host")
    if port:
        env["PENGUWATCH_PORT"] = str(int(port))
    return env


_UNIT_ENV = re.compile(r'^Environment="?(PENGUWATCH_HOST|PENGUWATCH_PORT)=([^"\n]*)"?\s*$', re.MULTILINE)
_TASK_ARG = re.compile(r'--(host|port)\s+("[^"]*"|\S+)')


def existing_settings(kind: str, scope: str, *, home: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
                      run: Optional[Callable] = None) -> Dict[str, str]:
    """The listen address (PENGUWATCH_HOST, PENGUWATCH_PORT) an installed service was given with --host and --port,
    read from its definition; {} when there is none or it sets neither. An upgrade passes them on, so installing again
    does not quietly close the dashboard to the other devices."""
    home = home or Path.home()
    found: Dict[str, str] = {}
    try:
        if kind == "linux":
            path = Path(SYSTEM_UNIT_PATH) if scope == SYSTEM else systemd_user_unit_path(home, env)
            for key, value in _UNIT_ENV.findall(path.read_text(encoding="utf-8")):
                found[key] = value.replace("%%", "%")
        elif kind == "macos":
            path = Path(MAC_DAEMON_PLIST) if scope == SYSTEM else launch_agent_path(home)
            with open(path, "rb") as handle:
                variables = plistlib.load(handle).get("EnvironmentVariables") or {}
            found = {k: str(v) for k, v in variables.items() if k in ("PENGUWATCH_HOST", "PENGUWATCH_PORT")}
        else:
            done = (run or default_runner)(["schtasks", "/Query", "/TN", TASK, "/XML"])
            if done.returncode == 0:
                from xml.sax.saxutils import unescape
                match = re.search(r"<Arguments>(.*?)</Arguments>", done.stdout, re.DOTALL)
                for name, value in _TASK_ARG.findall(unescape(match.group(1)) if match else ""):
                    found["PENGUWATCH_" + name.upper()] = value.strip('"')
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    if "PENGUWATCH_PORT" in found and not found["PENGUWATCH_PORT"].isdigit():
        del found["PENGUWATCH_PORT"]
    return found


def system_locations(kind: str) -> Tuple[str, str]:
    """(data folder, config file) of the system service."""
    if kind == "linux":
        return SYSTEM_DATA_DIR, f"{SYSTEM_CONFIG_DIR}/{paths.CONFIG_FILE}"
    if kind == "macos":
        return MAC_DAEMON_DATA_DIR, f"{MAC_DAEMON_DATA_DIR}/{paths.CONFIG_FILE}"
    raise ServiceError("Windows has no system service: its task runs as you, with the files in the usual places")


def system_kind(platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "macos"
    if platform == "win32":
        return "windows"
    raise ServiceError(f"no service support for {platform}; run `penguwatch serve` yourself")


def default_scope(kind: str, is_root: bool) -> str:
    """Linux and macOS: the system service for root, the per-user one otherwise. Windows has only the per-user task."""
    return SYSTEM if kind != "windows" and is_root else USER


def systemd_user_unit_path(home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME")
    config = Path(base) if base and Path(base).is_absolute() else home / ".config"
    return config / "systemd" / "user" / UNIT


def launch_agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def user_config_dir(kind: str) -> Path:
    """Where the per-user config file and settings file are (what the `penguwatch` command looks at)."""
    return paths.default_config_dir({**os.environ}, {"linux": "linux", "macos": "darwin"}.get(kind, "win32"))


def settings_file_for(kind: str, scope: str) -> str:
    """The penguwatch.env a service of this kind and scope loads."""
    if scope == SYSTEM:
        return str(PurePosixPath(system_locations(kind)[1]).parent / paths.ENV_FILE)
    return str(user_config_dir(kind) / paths.ENV_FILE)


def _exe(python: Optional[str], windows: bool) -> str:
    """The console script next to the interpreter running this: `penguwatch` (`penguwatchw` on Windows, which starts
    without a console window)."""
    name = "penguwatchw.exe" if windows else NAME
    if python is None:  # the interpreter running this: a path of this host, whatever system the plan is for
        return _no_control(str(Path(sys.executable).parent / name), "The path of penguwatch")
    pure = PureWindowsPath if windows else PurePosixPath
    return _no_control(str(pure(python).parent / name), "The path of penguwatch")


def _seed_steps(config: str, env_file: str, owner: Optional[str] = None) -> List[Step]:
    """The config file (the example) and the settings file (empty template, owner-only) are created when missing,
    never replaced."""
    example = EXAMPLE_CONFIG.read_text(encoding="utf-8") if EXAMPLE_CONFIG.is_file() else ""
    return [Step("write_new", config, example, 0o644 if owner is None else 0o600, owner=owner),
            Step("write_new", env_file, envfile.TEMPLATE, 0o600, owner=owner)]


def _settings_note(env_file: str) -> str:
    return (f"Settings that are secrets (the access token, a notification URL) go in {env_file}; "
            "restart the service after changing it.")


def plan_install(
    kind: str, scope: str, *, python: Optional[str] = None, home: Optional[Path] = None, host: Optional[str] = None,
    port: Optional[int] = None, account: Optional[str] = None, env: Optional[Dict[str, str]] = None,
    linger: Optional[bool] = None, config_dir: Optional[Path] = None,
) -> Plan:
    """What `service install` does for this OS and scope. `python` is the interpreter whose neighbour is the
    console script (default: the running one); `linger` is whether the user's services survive logout (Linux user
    unit; None = unknown); `config_dir` is the per-user config folder (default: what `penguwatch paths` shows)."""
    home = home or Path.home()
    extra = _env_for(host, port)
    user_dir = str(config_dir) if config_dir is not None else str(user_config_dir(kind))
    user_seed = _seed_steps(str(Path(user_dir) / paths.CONFIG_FILE) if kind != "windows"
                            else str(PureWindowsPath(user_dir) / paths.CONFIG_FILE),
                            str(Path(user_dir) / paths.ENV_FILE) if kind != "windows"
                            else str(PureWindowsPath(user_dir) / paths.ENV_FILE))
    if kind == "linux" and scope == SYSTEM:
        exe = _exe(python, False)
        text = render_systemd_system(exe, extra)
        config_file, env_file = system_locations(kind)[1], settings_file_for(kind, SYSTEM)
        return Plan("systemd-system", [
            Step("mkdir", SYSTEM_CONFIG_DIR, mode=0o755),
            *_seed_steps(config_file, env_file),
            Step("write", str(SYSTEM_UNIT_PATH), text, 0o644),
            Step("run", argv=["systemctl", "daemon-reload"]),
            Step("run", argv=["systemctl", "enable", UNIT]),
            Step("run", argv=["systemctl", "restart", UNIT]),
        ], [f"The service runs as a throw-away user; its data is in {SYSTEM_DATA_DIR}, its config in {config_file}.",
            _settings_note(env_file)], {str(SYSTEM_UNIT_PATH): text})
    if kind == "linux":
        exe = _exe(python, False)
        unit = systemd_user_unit_path(home, env)
        text = render_systemd_user(exe, extra)
        notes = [_settings_note(str(Path(user_dir) / paths.ENV_FILE))]
        if "/uv/tools/" in exe:
            notes.append("After `uv tool install` of a newer version, run `penguwatch service install` again to restart it on the new "
                         "version. Before `uv tool uninstall penguwatch`, run `penguwatch service uninstall --user`.")
        if linger is False:
            notes.insert(0, "It stops when you log out. To keep it running: sudo loginctl enable-linger "
                         + (account or "$USER"))
        return Plan("systemd-user", [
            *user_seed,
            Step("write", str(unit), text, 0o644),
            Step("run", argv=["systemctl", "--user", "daemon-reload"]),
            Step("run", argv=["systemctl", "--user", "enable", UNIT]),
            Step("run", argv=["systemctl", "--user", "restart", UNIT]),
        ], notes, {str(unit): text})
    if kind == "macos" and scope == SYSTEM:
        exe = _exe(python, False)
        data = MAC_DAEMON_DATA_DIR
        log = f"{data}/{paths.LOG_FILE}"
        config_file, env_file = system_locations(kind)[1], settings_file_for(kind, SYSTEM)
        text = render_launchd(exe, {"PENGUWATCH_DATA": data, "PENGUWATCH_CONFIG": config_file, **extra}, log, MAC_USER)
        return Plan("launchd-daemon", [
            Step("mac_user"),
            Step("mkdir", data, mode=0o700, owner=MAC_USER),
            *_seed_steps(config_file, env_file, owner=MAC_USER),
            Step("write", str(MAC_DAEMON_PLIST), text, 0o644),
            Step("chown", str(MAC_DAEMON_PLIST), owner="root:wheel"),
            Step("run", argv=["launchctl", "bootout", f"system/{LABEL}"], tolerate=True),
            Step("run", argv=["launchctl", "bootstrap", "system", str(MAC_DAEMON_PLIST)]),
            Step("run", argv=["launchctl", "enable", f"system/{LABEL}"]),
            Step("run", argv=["launchctl", "kickstart", "-k", f"system/{LABEL}"], tolerate=True),
        ], [f"It runs as the account {MAC_USER}; its data is in {data}. Log: {log}", _settings_note(env_file)],
            {str(MAC_DAEMON_PLIST): text})
    if kind == "macos":
        exe = _exe(python, False)
        plist = launch_agent_path(home)
        log = str(home / "Library" / "Logs" / "PenguWatch" / paths.LOG_FILE)
        text = render_launchd(exe, extra, log)
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
        return Plan("launchd-agent", [
            Step("mkdir", str(Path(log).parent), mode=0o700),
            *user_seed,
            Step("write", str(plist), text, 0o644),
            Step("run", argv=["launchctl", "bootout", f"{domain}/{LABEL}"], tolerate=True),
            Step("run", argv=["launchctl", "bootstrap", domain, str(plist)]),
            Step("run", argv=["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], tolerate=True),
        ], [f"It starts when you log in. Log: {log}", _settings_note(str(Path(user_dir) / paths.ENV_FILE))],
            {str(plist): text})
    # Windows
    exe = _exe(python, True)
    arguments = "serve" + (f" --host {_win_arg(host)}" if host else "") + (f" --port {int(port)}" if port else "")
    xml = render_task_xml(exe, arguments, account or windows_account(), str(PureWindowsPath(exe).parent))
    return Plan("windows-task", [
        Step("run", argv=["schtasks", "/End", "/TN", TASK], tolerate=True),
        Step("run", argv=list(TASKKILL), tolerate=True),
        *user_seed,
        Step("write", "<task.xml>", xml, 0o600),
        Step("run", argv=["schtasks", "/Create", "/TN", TASK, "/XML", "<task.xml>", "/F"]),
        Step("run", argv=["schtasks", "/Run", "/TN", TASK]),
    ], ["It starts at every sign-in, without a console window.",
        _settings_note(str(PureWindowsPath(user_dir) / paths.ENV_FILE))], {"<task.xml>": xml})


def plan_uninstall(kind: str, scope: str, *, home: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Plan:
    """What `service uninstall` does. The data folder, the config and settings files and the venv are left alone."""
    home = home or Path.home()
    if kind == "linux" and scope == SYSTEM:
        return Plan("systemd-system", [
            Step("run", argv=["systemctl", "disable", "--now", UNIT], tolerate=True),
            Step("remove", str(SYSTEM_UNIT_PATH)),
            Step("run", argv=["systemctl", "daemon-reload"]),
        ], [f"Your data is kept in {SYSTEM_DATA_DIR}, your config and settings in {SYSTEM_CONFIG_DIR}."])
    kept = f"Your data is kept in {paths.data_dir()}, your config and settings in {user_config_dir(kind)}."
    if kind == "linux":
        unit = systemd_user_unit_path(home, env)
        return Plan("systemd-user", [
            Step("run", argv=["systemctl", "--user", "disable", "--now", UNIT], tolerate=True),
            Step("remove", str(unit)),
            Step("run", argv=["systemctl", "--user", "daemon-reload"]),
        ], [kept])
    if kind == "macos" and scope == SYSTEM:
        return Plan("launchd-daemon", [
            Step("run", argv=["launchctl", "bootout", f"system/{LABEL}"], tolerate=True),
            Step("remove", str(MAC_DAEMON_PLIST)),
        ], [f"Your data, config and settings are kept in {MAC_DAEMON_DATA_DIR}, and the account {MAC_USER} stays "
            "(it owns them)."])
    if kind == "macos":
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
        return Plan("launchd-agent", [
            Step("run", argv=["launchctl", "bootout", f"{domain}/{LABEL}"], tolerate=True),
            Step("remove", str(launch_agent_path(home))),
        ], [kept])
    return Plan("windows-task", [
        Step("run", argv=["schtasks", "/End", "/TN", TASK], tolerate=True),
        Step("run", argv=list(TASKKILL), tolerate=True),
        Step("run", argv=["schtasks", "/Delete", "/TN", TASK, "/F"]),
    ], [kept])


def windows_account(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    user = env.get("USERNAME") or getpass.getuser()
    domain = env.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


# ---- carrying a plan out ---------------------------------------------------------------------------------------------

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _write(path: Path, text: str, mode: int, exclusive: bool = False) -> bool:
    """Writes the file with its final mode from the first byte (never world-readable for a moment). With `exclusive`
    an existing file is left alone and False comes back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(path, mode)
    return True


def _ensure_mac_user(run: Runner) -> None:
    if run(["dscl", ".", "-read", f"/Users/{MAC_USER}", "UniqueID"]).returncode == 0:
        return
    listing = run(["dscl", ".", "-list", "/Users", "UniqueID"]).stdout
    used = {int(parts[1]) for parts in (line.split() for line in listing.splitlines()) if len(parts) == 2 and parts[1].isdigit()}
    uid = next((n for n in range(450, 500) if n not in used), None)  # the range Apple's service accounts use
    if uid is None:
        raise ServiceError(f"no free user id between 450 and 499 for the account {MAC_USER}")
    path, group = f"/Users/{MAC_USER}", f"/Groups/{MAC_USER}"
    for argv in (
        ["dscl", ".", "-create", group],
        ["dscl", ".", "-create", group, "PrimaryGroupID", str(uid)],
        ["dscl", ".", "-create", path],
        ["dscl", ".", "-create", path, "UniqueID", str(uid)],
        ["dscl", ".", "-create", path, "PrimaryGroupID", str(uid)],
        ["dscl", ".", "-create", path, "UserShell", "/usr/bin/false"],
        ["dscl", ".", "-create", path, "RealName", "PenguWatch"],
        ["dscl", ".", "-create", path, "NFSHomeDirectory", "/var/empty"],
        ["dscl", ".", "-create", path, "IsHidden", "1"],
    ):
        done = run(argv)
        if done.returncode != 0:
            raise ServiceError(f"{' '.join(argv)} failed: {(done.stderr or done.stdout).strip()}")


def run_plan(plan: Plan, run: Runner = default_runner, out=None) -> None:
    """Carries the steps out in order; the first failing step (one that is not `tolerate`) stops with a ServiceError."""
    out = out or sys.stdout
    task_file: Optional[Path] = None
    try:
        for step in plan.steps:
            print(f"  {step.describe()}", file=out)
            if step.kind == "write" and step.path == "<task.xml>":
                handle, name = tempfile.mkstemp(suffix=".xml", prefix="penguwatch-task-")
                os.close(handle)
                task_file = Path(name)
                task_file.write_bytes(b"\xff\xfe" + str(step.text).encode("utf-16-le"))  # Task Scheduler wants UTF-16
            elif step.kind in ("write", "write_new"):
                created = _write(Path(step.path), step.text or "", step.mode, exclusive=step.kind == "write_new")
                if created and step.owner:
                    shutil.chown(step.path, user=step.owner)
            elif step.kind == "mkdir":
                Path(step.path).mkdir(mode=step.mode, parents=True, exist_ok=True)
                if step.owner:
                    shutil.chown(step.path, user=step.owner)
                os.chmod(step.path, step.mode)
            elif step.kind == "chown":
                done = run(["chown", step.owner, step.path])
                if done.returncode != 0:
                    raise ServiceError(f"chown {step.owner} {step.path} failed: {done.stderr.strip()}")
            elif step.kind == "remove":
                Path(step.path).unlink(missing_ok=True)
            elif step.kind == "mac_user":
                _ensure_mac_user(run)
            else:
                argv = [str(task_file) if a == "<task.xml>" and task_file else a for a in step.argv]
                done = run(argv)
                if done.returncode != 0 and not step.tolerate:
                    raise ServiceError(f"{' '.join(argv)} failed ({done.returncode}): "
                                       f"{(done.stderr or done.stdout).strip() or 'no output'}")
    finally:
        if task_file:
            task_file.unlink(missing_ok=True)
    for note in plan.notes:
        print(note, file=out)


# ---- status ---------------------------------------------------------------------------------------------------------

def health_body(host: str, port: int, timeout: float = 3.0) -> Optional[dict]:
    """What /api/health says ({"status": "ok"}), or None when nothing healthy answers."""
    import json

    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in shown:
        shown = f"[{shown}]"
    try:
        with urllib.request.urlopen(f"http://{shown}:{port}/api/health", timeout=timeout) as reply:
            body = json.loads(reply.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


def installed_scopes(kind: str, home: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
                     run: Runner = default_runner) -> List[str]:
    """Which services exist on this machine: [] , ["system"], ["user"] or both."""
    home = home or Path.home()
    found = []
    if kind == "linux":
        if Path(SYSTEM_UNIT_PATH).is_file():
            found.append(SYSTEM)
        if systemd_user_unit_path(home, env).is_file():
            found.append(USER)
    elif kind == "macos":
        if Path(MAC_DAEMON_PLIST).is_file():
            found.append(SYSTEM)
        if launch_agent_path(home).is_file():
            found.append(USER)
    elif run(["schtasks", "/Query", "/TN", TASK]).returncode == 0:
        found.append(USER)
    return found


def manager_state(kind: str, scope: str, run: Runner = default_runner) -> str:
    """What the service manager says: "running", "stopped" or "unknown"."""
    if kind == "linux":
        argv = ["systemctl", *(["--user"] if scope == USER else []), "is-active", UNIT]
        return "running" if run(argv).stdout.strip() == "active" else "stopped"
    if kind == "macos":
        domain = "system" if scope == SYSTEM else f"gui/{os.getuid()}"
        done = run(["launchctl", "print", f"{domain}/{LABEL}"])
        if done.returncode != 0:
            return "stopped"
        return "running" if "state = running" in done.stdout else "stopped"
    done = run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"])
    return "running" if "Running" in done.stdout else "stopped" if done.returncode == 0 else "unknown"


# ---- command line ----------------------------------------------------------------------------------------------------

def _scope_from(args, kind: str) -> str:
    if getattr(args, "user", False) and getattr(args, "system", False):
        raise ServiceError("--user and --system exclude each other")
    if getattr(args, "system", False):
        return SYSTEM
    if getattr(args, "user", False):
        return USER
    return default_scope(kind, hasattr(os, "geteuid") and os.geteuid() == 0)


def _require_root(kind: str, scope: str, action: str) -> None:
    if scope == SYSTEM and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ServiceError(f"{action} the system service needs root: run it with sudo, "
                           "or add --user for a service that runs as you")
    if kind == "linux" and scope == USER and hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ServiceError("--user as root would install into root's own user manager; leave --user out for "
                           "the system service, or run it as the user who should own the service")


def _linger(run: Runner) -> Optional[bool]:
    done = run(["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"])
    if done.returncode != 0:
        return None
    return done.stdout.strip() == "yes"


def cmd_install(args, run: Runner = default_runner) -> int:
    kind = system_kind()
    scope = _scope_from(args, kind)
    if not args.dry_run:
        _require_root(kind, scope, "installing")
    exe = _exe(None, kind == "windows")
    if not args.dry_run and not Path(exe).exists():
        raise ServiceError(f"{exe} does not exist: run this from the penguwatch that is installed in a virtual environment")
    home = Path.home()
    kept = existing_settings(kind, scope, home=home, run=run)
    host = args.host or kept.get("PENGUWATCH_HOST")
    port = args.port or (int(kept["PENGUWATCH_PORT"]) if kept.get("PENGUWATCH_PORT") else None)
    if (host, port) != (args.host, args.port):
        kept_words = ", ".join(w for w in (f"host {host}" if host != args.host else "",
                                           f"port {port}" if port != args.port else "") if w)
        print(f"Keeping the installed service's listen address ({kept_words}). Add --host or --port to change it.")
    plan = plan_install(kind, scope, home=home, host=host, port=port,
                        account=getpass.getuser(), linger=_linger(run) if kind == "linux" and scope == USER else None)
    print(f"{'Would install' if args.dry_run else 'Installing'} PenguWatch {__version__} as a service ({plan.mode}):")
    if args.dry_run:
        for step in plan.steps:
            print(f"  {step.describe()}")
        for name, text in plan.files.items():
            print(f"\n--- {name} ---\n{text.rstrip()}")
        for note in plan.notes:
            print(note)
        return 0
    run_plan(plan, run)
    shown, shown_port, _, _ = paths.listen_address(None, {**os.environ, **_env_for(host, port)})
    where = "127.0.0.1" if shown in ("0.0.0.0", "::") else f"[{shown}]" if ":" in shown else shown
    print(f"PenguWatch is starting; open http://{where}:{shown_port}/")
    if shown in ("0.0.0.0", "::"):
        for line in _other_devices_lines(shown_port, _token_is_set(kind, scope, home)):
            print(line)
    return 0


def _token_is_set(kind: str, scope: str, home: Path) -> bool:
    """Whether an access token is already given to this service (or to this shell): its settings file, its unit, a
    systemd drop-in (`systemctl edit`), a launchd plist or the environment mention PENGUWATCH_ACCESS_TOKEN. Only the
    name is looked for; no value is read out or shown. False when unknown."""
    if os.environ.get("PENGUWATCH_ACCESS_TOKEN"):
        return True
    try:
        values, _ = envfile.parse(Path(settings_file_for(kind, scope)).read_text(encoding="utf-8"))
        if values.get("PENGUWATCH_ACCESS_TOKEN"):
            return True
    except (OSError, ServiceError):
        pass
    try:
        if kind == "linux":
            unit = Path(SYSTEM_UNIT_PATH) if scope == SYSTEM else systemd_user_unit_path(home)
            files = [unit, *sorted(unit.parent.joinpath(unit.name + ".d").glob("*.conf"))]
        elif kind == "macos":
            files = [Path(MAC_DAEMON_PLIST) if scope == SYSTEM else launch_agent_path(home)]
        else:
            return False
        return any("PENGUWATCH_ACCESS_TOKEN" in f.read_text(encoding="utf-8", errors="replace") for f in files)
    except OSError:
        return False


def lan_urls(port: int) -> List[str]:
    """The addresses other devices would use: this computer's name, and its address on the network (found by asking the
    system which interface reaches the outside; no packet is sent)."""
    import socket

    urls = []
    try:
        urls.append(f"http://{socket.gethostname()}:{port}/")
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent for a UDP connect
            address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            urls.append(f"http://{address}:{port}/")
    except OSError:
        pass
    return urls


def _other_devices_lines(port: int, has_token: bool) -> List[str]:
    """What to tell the user after `--host 0.0.0.0`: the address other devices use, then the token advice."""
    urls = lan_urls(port)
    lines = []
    if urls:
        lines.append("From other devices on your network, open " + " or ".join(urls))
    lines.append("It listens on every network connection of this computer, so anyone on the network can reach it."
                 + (" The access token you set protects it." if has_token
                    else " Set PENGUWATCH_ACCESS_TOKEN first (see docs/install.md and docs/security.md)."))
    return lines


def cmd_uninstall(args, run: Runner = default_runner) -> int:
    kind = system_kind()
    if getattr(args, "system", False) or getattr(args, "user", False):
        scopes = [_scope_from(args, kind)]
    else:
        scopes = installed_scopes(kind, run=run)
    if not scopes:
        print("PenguWatch is not installed as a service.")
        return 0
    for scope in scopes:
        if not args.dry_run:
            _require_root(kind, scope, "removing")
        plan = plan_uninstall(kind, scope)
        print(f"{'Would remove' if args.dry_run else 'Removing'} the PenguWatch service ({plan.mode}):")
        if args.dry_run:
            for step in plan.steps:
                print(f"  {step.describe()}")
            continue
        run_plan(plan, run)
    return 0


def _config_server_section(kind: str, scope: str) -> dict:
    """The `server` section of the config file this service reads (for the port it would listen on), or {}."""
    import yaml

    try:
        config = Path(system_locations(kind)[1]) if scope == SYSTEM else paths.config_path()
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        section = data.get("server") if isinstance(data, dict) else None
        return section if isinstance(section, dict) else {}
    except (OSError, ValueError, ServiceError, yaml.YAMLError):
        return {}


def cmd_status(args, run: Runner = default_runner) -> int:
    """0: installed, running and answering. 1: installed but not answering. 3: not installed."""
    kind = system_kind()
    scopes = installed_scopes(kind, run=run)
    if not scopes:
        print("PenguWatch is not installed as a service. Install it with: penguwatch service install")
        return 3
    for scope in scopes:
        print(f"{scope} service: {manager_state(kind, scope, run)}")
    listen_env = dict(os.environ)
    for scope in scopes:  # the address the service was installed with, unless this command is told another
        for key, value in existing_settings(kind, scope, run=run).items():
            listen_env.setdefault(key, value)
    host, port, _, _ = paths.listen_address(_config_server_section(kind, scopes[0]), listen_env)
    if health_body(host, port):
        print(f"answering on port {port}: PenguWatch (this program on disk is {__version__})")
        return 0
    print(f"nothing healthy answers on port {port} (if the service listens elsewhere, set PENGUWATCH_PORT for this command)")
    return 1


def add_parser(sub) -> None:
    """Adds `service` with its three subcommands to cli's subparsers."""
    service = sub.add_parser("service", help="start PenguWatch automatically: install, uninstall or show the status")
    actions = service.add_subparsers(dest="service_command", metavar="{install,uninstall,status}", required=True)
    install = actions.add_parser("install", help="install and start the background service")
    install.add_argument("--system", action="store_true", help="the system-wide service (needs root; the default as root)")
    install.add_argument("--user", action="store_true", help="a service that runs as you (the default when not root)")
    install.add_argument("--host", help=f"listen address (default {paths.DEFAULT_HOST}, this computer only)")
    install.add_argument("--port", type=int, help=f"listen port (default {paths.DEFAULT_PORT})")
    install.add_argument("--dry-run", action="store_true", help="show what would be done and the files, change nothing")
    install.set_defaults(func=cmd_install)
    uninstall = actions.add_parser("uninstall", help="stop and remove the service (keeps your data)")
    uninstall.add_argument("--system", action="store_true")
    uninstall.add_argument("--user", action="store_true")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.set_defaults(func=cmd_uninstall)
    actions.add_parser("status", help="is the service installed, running and answering").set_defaults(func=cmd_status)
