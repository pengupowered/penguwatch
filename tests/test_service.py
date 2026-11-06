"""`penguwatch service install | uninstall | status`: the definitions written for systemd, launchd and Task Scheduler,
the steps that carry them out (with a fake command runner: nothing here touches the real service manager), the
refusals, and that no secret ever lands in a definition or in the output. Everything runs on every OS; a test that
needs a Linux tool skips without it."""

import plistlib
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from penguwatch import cli, paths, service
from penguwatch.service import SYSTEM, USER

ROOT = Path(__file__).resolve().parents[1]
EXE = "/opt/penguwatch/venv/bin/penguwatch"
SECRET = "s3cr3t-token-value-9f8e7d"


def _unit(text: str) -> dict:
    """A systemd unit as {section: {key: [values]}} (keys repeat, so configparser's single value would hide lines)."""
    out, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = out.setdefault(line.strip("[]"), {})
        else:
            key, _, value = line.partition("=")
            section.setdefault(key, []).append(value)
    return out


class FakeRun:
    """Records the commands; `answers` maps the start of a command (a tuple) to (returncode, stdout)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        for prefix, (code, out) in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(list(argv), code, out, "boom" if code else "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT",
                 "PENGUWATCH_ENV_FILE", "PENGUWATCH_ACCESS_TOKEN", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


# ---- the Linux system unit --------------------------------------------------------------------------------------------

def test_system_unit_runs_as_a_throwaway_user_with_no_capability():
    section = _unit(service.render_systemd_system(EXE))["Service"]
    assert section["ExecStart"] == [f"{EXE} serve"]
    assert section["DynamicUser"] == ["yes"]
    assert section["StateDirectory"] == ["penguwatch"]
    assert section["CapabilityBoundingSet"] == [""]   # an empty bounding set: no capability at all
    assert section["AmbientCapabilities"] == [""]
    assert section["NoNewPrivileges"] == ["yes"]
    assert section["ProtectSystem"] == ["strict"]
    assert section["ProtectHome"] == ["yes"]
    assert section["UMask"] == ["0077"]
    assert section["Restart"] == ["on-failure"]
    assert "PENGUWATCH_DATA=/var/lib/penguwatch" in section["Environment"]
    assert "PENGUWATCH_CONFIG=/etc/penguwatch/config.yaml" in section["Environment"]
    assert section["EnvironmentFile"] == ["-/etc/penguwatch/penguwatch.env"]


def test_system_unit_has_no_capability_and_no_raw_sockets_whatever_the_input():
    text = service.render_systemd_system(EXE, {"PENGUWATCH_HOST": "0.0.0.0", "PENGUWATCH_PORT": "9000"})
    assert "CAP_" not in text
    families = _unit(text)["Service"]["RestrictAddressFamilies"][0].split()
    assert set(families) == {"AF_INET", "AF_INET6", "AF_UNIX"}  # no AF_NETLINK, AF_PACKET


def test_system_unit_carries_the_host_and_port_it_was_asked_for():
    env = _unit(service.render_systemd_system(EXE, {"PENGUWATCH_HOST": "0.0.0.0", "PENGUWATCH_PORT": "9000"}))["Service"]["Environment"]
    assert "PENGUWATCH_HOST=0.0.0.0" in env and "PENGUWATCH_PORT=9000" in env


def test_user_unit_has_no_sandboxing_that_needs_namespaces():
    section = _unit(service.render_systemd_user(EXE))["Service"]
    for key in ("ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices", "PrivateUsers", "DynamicUser",
                "StateDirectory", "RestrictNamespaces", "EnvironmentFile"):
        assert key not in section, key
    assert section["ExecStart"] == [f"{EXE} serve"] and section["NoNewPrivileges"] == ["yes"]
    assert _unit(service.render_systemd_user(EXE))["Install"]["WantedBy"] == ["default.target"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_system_unit_scores_well_in_systemd_analyze_security(tmp_path):
    """The exposure score the documentation quotes. The binary has to exist for the offline analysis."""
    exe = tmp_path / "penguwatch"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    unit = tmp_path / "penguwatch.service"
    unit.write_text(service.render_systemd_system(str(exe)))
    done = subprocess.run(["systemd-analyze", "security", "--offline=true", "--no-pager", str(unit)],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0 and "offline" in (done.stderr or ""):
        pytest.skip("this systemd-analyze has no --offline")
    assert done.returncode == 0, done.stderr
    line = next(ln for ln in done.stdout.splitlines() if "Overall exposure level" in ln)
    assert float(line.split(":")[1].split()[0]) <= service.MAX_EXPOSURE, line


# ---- macOS ---------------------------------------------------------------------------------------------------------------

def test_launchd_daemon_plist_runs_as_the_dedicated_account():
    plist = plistlib.loads(service.render_launchd(
        "/usr/local/penguwatch/venv/bin/penguwatch", {"PENGUWATCH_DATA": "/Library/Application Support/PenguWatch"},
        "/Library/Application Support/PenguWatch/penguwatch.log", service.MAC_USER).encode())
    assert plist["Label"] == "com.pengupowered.penguwatch"
    assert plist["ProgramArguments"] == ["/usr/local/penguwatch/venv/bin/penguwatch", "serve"]
    assert plist["UserName"] == "_penguwatch" and plist["GroupName"] == "_penguwatch"
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["EnvironmentVariables"]["PENGUWATCH_DATA"] == "/Library/Application Support/PenguWatch"


def test_launchd_agent_plist_has_no_account():
    plist = plistlib.loads(service.render_launchd("/x/penguwatch", {}, "/x/log").encode())
    assert "UserName" not in plist and plist["ProgramArguments"] == ["/x/penguwatch", "serve"]


def test_mac_account_is_created_with_a_free_id_and_no_login():
    run = FakeRun({("dscl", ".", "-read"): (1, ""), ("dscl", ".", "-list"): (0, "root 0\n_www 70\n_penguwatch_x 450\nbo 501\n")})
    service._ensure_mac_user(run)
    created = [c for c in run.calls if "-create" in c]
    assert ["dscl", ".", "-create", "/Users/_penguwatch", "UniqueID", "451"] in created
    assert ["dscl", ".", "-create", "/Users/_penguwatch", "UserShell", "/usr/bin/false"] in created
    assert ["dscl", ".", "-create", "/Users/_penguwatch", "IsHidden", "1"] in created


def test_mac_account_that_exists_is_left_alone():
    run = FakeRun()
    service._ensure_mac_user(run)
    assert run.calls == [["dscl", ".", "-read", "/Users/_penguwatch", "UniqueID"]]


# ---- Windows -------------------------------------------------------------------------------------------------------------

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_starts_at_logon_for_the_user_without_admin_rights():
    root = ET.fromstring(service.render_task_xml(r"C:\P\Scripts\penguwatchw.exe", "serve --port 9000", r"PC\bo", r"C:\P\Scripts").split("\n", 1)[1])
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == r"PC\bo"
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == r"C:\P\Scripts\penguwatchw.exe"
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "serve --port 9000"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:RegistrationInfo/t:URI", NS).text == "\\PenguWatch"


def test_task_xml_escapes_names():
    text = service.render_task_xml(r"C:\A & B\penguwatchw.exe", 'serve --host "a<b"', "PC\\x&y", r"C:\A & B")
    ET.fromstring(text.split("\n", 1)[1])  # still well-formed
    assert "A &amp; B" in text and "x&amp;y" in text


def test_windows_account_is_domain_and_user():
    assert service.windows_account({"USERNAME": "bo", "USERDOMAIN": "PC"}) == "PC\\bo"
    assert service.windows_account({"USERNAME": "bo"}) == "bo"


# ---- the plans -------------------------------------------------------------------------------------------------------------

def _commands(plan):
    return [list(s.argv) for s in plan.steps if s.kind == "run"]


def test_linux_system_plan_seeds_config_and_settings_then_writes_the_unit_then_enables_and_restarts_it():
    plan = service.plan_install("linux", SYSTEM, python="/opt/penguwatch/venv/bin/python")
    kinds = [(s.kind, s.path) for s in plan.steps if s.kind != "run"]
    assert kinds == [("mkdir", "/etc/penguwatch"), ("write_new", "/etc/penguwatch/config.yaml"),
                     ("write_new", "/etc/penguwatch/penguwatch.env"), ("write", "/etc/systemd/system/penguwatch.service")]
    env_step = next(s for s in plan.steps if s.path == "/etc/penguwatch/penguwatch.env")
    assert env_step.mode == 0o600 and env_step.text == service.envfile.TEMPLATE
    assert _commands(plan) == [["systemctl", "daemon-reload"], ["systemctl", "enable", "penguwatch.service"],
                               ["systemctl", "restart", "penguwatch.service"]]
    assert plan.files["/etc/systemd/system/penguwatch.service"] == service.render_systemd_system(EXE)


def test_linux_user_plan_uses_the_user_manager_and_the_xdg_config_home(tmp_path):
    plan = service.plan_install("linux", USER, python="/home/bo/.local/share/penguwatch/app/venv/bin/python", home=tmp_path,
                                env={"XDG_CONFIG_HOME": str(tmp_path / "cfg")}, linger=False, account="bo")
    unit = tmp_path / "cfg" / "systemd" / "user" / "penguwatch.service"
    assert [s.path for s in plan.steps if s.kind == "write"] == [str(unit)]
    assert _commands(plan)[-1] == ["systemctl", "--user", "restart", "penguwatch.service"]
    assert any("enable-linger bo" in n for n in plan.notes)
    assert any("penguwatch.env" in n for n in plan.notes)


def test_macos_daemon_plan_creates_the_account_and_data_directory_before_loading():
    plan = service.plan_install("macos", SYSTEM, python="/usr/local/penguwatch/venv/bin/python")
    kinds = [s.kind for s in plan.steps]
    assert kinds[0] == "mac_user" and kinds[1] == "mkdir"
    mkdir = plan.steps[1]
    assert mkdir.path == "/Library/Application Support/PenguWatch" and mkdir.mode == 0o700 and mkdir.owner == "_penguwatch"
    seeds = [s for s in plan.steps if s.kind == "write_new"]
    assert {s.path for s in seeds} == {"/Library/Application Support/PenguWatch/config.yaml",
                                       "/Library/Application Support/PenguWatch/penguwatch.env"}
    assert all(s.owner == "_penguwatch" and s.mode == 0o600 for s in seeds)  # the settings are private to the account
    plist = plistlib.loads(plan.files["/Library/LaunchDaemons/com.pengupowered.penguwatch.plist"].encode())
    assert plist["EnvironmentVariables"]["PENGUWATCH_CONFIG"] == "/Library/Application Support/PenguWatch/config.yaml"
    boot = _commands(plan)
    assert boot.index(["launchctl", "bootout", "system/com.pengupowered.penguwatch"]) < boot.index(
        ["launchctl", "bootstrap", "system", "/Library/LaunchDaemons/com.pengupowered.penguwatch.plist"])


def test_macos_agent_plan_lives_in_the_users_library(tmp_path):
    plan = service.plan_install("macos", USER, python="/x/venv/bin/python", home=tmp_path, config_dir=tmp_path / "cfg")
    plist = tmp_path / "Library" / "LaunchAgents" / "com.pengupowered.penguwatch.plist"
    assert str(plist) in plan.files
    assert any(s.kind == "write" and s.path == str(plist) for s in plan.steps)
    assert plistlib.loads(plan.files[str(plist)].encode()).get("UserName") is None
    assert str(tmp_path / "cfg" / "penguwatch.env") in " ".join(plan.notes)


def test_windows_plan_registers_a_task_from_an_xml_file_and_starts_it():
    plan = service.plan_install("windows", USER, python=r"C:\P\venv\Scripts\python.exe", account="PC\\bo",
                                config_dir=PureWindowsPath(r"C:\Users\bo\AppData\Roaming\PenguWatch"))
    commands = _commands(plan)
    assert commands[-2][:2] == ["schtasks", "/Create"] and commands[-1] == ["schtasks", "/Run", "/TN", "PenguWatch"]
    xml = plan.files["<task.xml>"]
    assert r"C:\P\venv\Scripts\penguwatchw.exe" in xml and "<Arguments>serve</Arguments>" in xml
    assert {s.path for s in plan.steps if s.kind == "write_new"} == {
        r"C:\Users\bo\AppData\Roaming\PenguWatch\config.yaml", r"C:\Users\bo\AppData\Roaming\PenguWatch\penguwatch.env"}


def test_uninstall_plans_stop_before_removing_and_keep_the_data():
    for kind, scope in (("linux", SYSTEM), ("linux", USER), ("macos", SYSTEM), ("macos", USER), ("windows", USER)):
        plan = service.plan_uninstall(kind, scope)
        assert any("kept" in n for n in plan.notes), (kind, scope)
        assert not any(s.kind == "remove" and s.path.endswith(("config.yaml", "penguwatch.env", ".db"))
                       for s in plan.steps)
    system = service.plan_uninstall("linux", SYSTEM)
    assert system.steps[0].argv[:3] == ["systemctl", "disable", "--now"]
    assert [s.path for s in system.steps if s.kind == "remove"] == ["/etc/systemd/system/penguwatch.service"]


def test_windows_install_and_uninstall_end_the_launcher_and_its_python_child():
    """schtasks /End can leave the python process running, which keeps the venv's files open (an upgrade or uninstall
    then fails half way)."""
    for plan in (service.plan_install("windows", USER, python=r"C:\P\Scripts\python.exe", account="PC\\bo",
                                      config_dir=PureWindowsPath(r"C:\C")),
                 service.plan_uninstall("windows", USER)):
        commands = _commands(plan)
        assert ["taskkill", "/F", "/T", "/IM", "penguwatchw.exe"] in commands
        assert commands.index(["schtasks", "/End", "/TN", "PenguWatch"]) < commands.index(list(service.TASKKILL))


def test_plans_for_another_system_use_that_systems_separators():
    assert service._exe(r"C:\P\Scripts\python.exe", True) == r"C:\P\Scripts\penguwatchw.exe"
    assert service._exe("/opt/penguwatch/venv/bin/python", False) == "/opt/penguwatch/venv/bin/penguwatch"
    assert str(service.SYSTEM_UNIT_PATH) == "/etc/systemd/system/penguwatch.service"
    assert isinstance(service.SYSTEM_UNIT_PATH, PurePosixPath)


# ---- carrying a plan out -------------------------------------------------------------------------------------------------

def test_run_plan_writes_the_file_with_its_mode_then_runs_the_commands_in_order(tmp_path, capsys):
    target = tmp_path / "sub" / "u.service"
    plan = service.Plan("t", [service.Step("write", str(target), "unit text", 0o600), service.Step("run", argv=["a"]),
                              service.Step("run", argv=["b"])], ["done"])
    run = FakeRun()
    service.run_plan(plan, run)
    assert target.read_text() == "unit text"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
    assert run.calls == [["a"], ["b"]]
    assert "done" in capsys.readouterr().out


def test_write_new_creates_a_missing_file_and_never_replaces_one(tmp_path):
    target = tmp_path / "cfg" / "penguwatch.env"
    plan = service.Plan("t", [service.Step("write_new", str(target), "PENGUWATCH_ACCESS_TOKEN=\n", 0o600)])
    service.run_plan(plan, FakeRun())
    assert target.read_text() == "PENGUWATCH_ACCESS_TOKEN=\n"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
    target.write_text(f"PENGUWATCH_ACCESS_TOKEN={SECRET}\n")
    service.run_plan(plan, FakeRun())
    assert target.read_text() == f"PENGUWATCH_ACCESS_TOKEN={SECRET}\n"  # an upgrade keeps what you wrote


def test_run_plan_stops_at_the_first_failure_but_tolerates_marked_steps():
    plan = service.Plan("t", [service.Step("run", argv=["a"], tolerate=True), service.Step("run", argv=["b"]),
                              service.Step("run", argv=["c"])])
    run = FakeRun({("a",): (1, ""), ("b",): (5, "")})
    with pytest.raises(service.ServiceError, match=r"b failed \(5\): boom"):
        service.run_plan(plan, run)
    assert run.calls == [["a"], ["b"]]


def test_run_plan_gives_task_scheduler_a_utf16_file_and_removes_it():
    plan = service.Plan("windows-task", [service.Step("write", "<task.xml>", "<Task>\u00fc</Task>", 0o600),
                                         service.Step("run", argv=["schtasks", "/Create", "/XML", "<task.xml>"])])
    seen = {}

    def run(argv):
        path = Path(argv[-1])
        seen["raw"] = path.read_bytes()
        seen["path"] = path
        return subprocess.CompletedProcess(argv, 0, "", "")

    service.run_plan(plan, run)
    assert seen["raw"].startswith(b"\xff\xfe") and seen["raw"][2:].decode("utf-16-le") == "<Task>\u00fc</Task>"
    assert not seen["path"].exists()


def test_a_missing_tool_is_a_failed_step_not_a_traceback():
    assert service.default_runner(["definitely-not-a-program-penguwatch"]).returncode == 127


# ---- the command line --------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_linux(monkeypatch):
    monkeypatch.setattr(service, "system_kind", lambda platform=None: "linux")


def test_install_dry_run_prints_the_unit_and_changes_nothing(as_linux, capsys, monkeypatch):
    monkeypatch.setattr(service, "run_plan", lambda *a, **k: pytest.fail("a dry run must not run anything"))
    assert cli.main(["service", "install", "--dry-run", "--system", "--port", "9100"]) == 0
    out = capsys.readouterr().out
    assert "Would install PenguWatch" in out and "/etc/systemd/system/penguwatch.service" in out
    assert "PENGUWATCH_PORT=9100" in out and "CapabilityBoundingSet=" in out and "DynamicUser=yes" in out


def test_install_system_without_root_says_to_use_sudo_or_user(as_linux, monkeypatch, capsys):
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(["service", "install", "--system"]) == 1
    err = capsys.readouterr().err
    assert "needs root" in err and "--user" in err


def test_install_user_as_root_is_refused(as_linux, monkeypatch, capsys):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0, raising=False)
    assert cli.main(["service", "install", "--user"]) == 1
    assert "root" in capsys.readouterr().err


def test_system_and_user_exclude_each_other(as_linux, capsys):
    assert cli.main(["service", "install", "--system", "--user", "--dry-run"]) == 1
    assert "exclude each other" in capsys.readouterr().err


def test_install_refuses_when_the_console_script_is_not_there(as_linux, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(service.sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(["service", "install", "--user"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_install_as_root_installs_the_system_service(as_linux, monkeypatch, tmp_path, capsys):
    scripts = tmp_path / "venv" / "bin"
    scripts.mkdir(parents=True)
    (scripts / "penguwatch").write_text("#!/bin/sh\n")
    monkeypatch.setattr(service.sys, "executable", str(scripts / "python"))
    monkeypatch.setattr(service.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", tmp_path / "etc" / "penguwatch.service")
    monkeypatch.setattr(service, "SYSTEM_CONFIG_DIR", str(tmp_path / "etc-penguwatch"))
    run = FakeRun()
    plans = []
    monkeypatch.setattr(service, "run_plan", lambda plan, runner=None, out=None: plans.append(plan))
    assert service.cmd_install(cli.build_parser().parse_args(["service", "install", "--port", "9100"]), run) == 0
    assert plans and plans[0].mode == "systemd-system"
    out = capsys.readouterr().out
    assert "open http://127.0.0.1:9100/" in out   # the installer reads the address from this line


def test_install_again_keeps_the_host_and_port_of_the_installed_service(as_linux, monkeypatch, tmp_path, capsys):
    unit = tmp_path / "penguwatch.service"
    unit.write_text(service.render_systemd_system(EXE, {"PENGUWATCH_HOST": "0.0.0.0", "PENGUWATCH_PORT": "9100"}))
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", unit)
    assert service.existing_settings("linux", SYSTEM) == {"PENGUWATCH_HOST": "0.0.0.0", "PENGUWATCH_PORT": "9100"}
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(["service", "install", "--system", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Keeping the installed service's listen address (host 0.0.0.0, port 9100)" in out
    assert "PENGUWATCH_HOST=0.0.0.0" in out and "PENGUWATCH_PORT=9100" in out


def test_the_settings_of_an_installed_service_are_read_from_each_kind_of_definition(tmp_path):
    agent = service.launch_agent_path(tmp_path)
    agent.parent.mkdir(parents=True)
    agent.write_text(service.render_launchd("/x/penguwatch", {"PENGUWATCH_PORT": "9300"}, "/x/log"))
    assert service.existing_settings("macos", USER, home=tmp_path) == {"PENGUWATCH_PORT": "9300"}
    xml = service.render_task_xml("c.exe", 'serve --host "a b" --port 9400', "PC\\bo", "C:\\")
    run = FakeRun({("schtasks", "/Query"): (0, xml)})
    assert service.existing_settings("windows", USER, run=run) == {"PENGUWATCH_HOST": "a b", "PENGUWATCH_PORT": "9400"}
    assert service.existing_settings("linux", USER, home=tmp_path) == {}  # no unit: nothing kept


def test_the_units_start_only_while_the_program_exists():
    for text in (service.render_systemd_system(EXE), service.render_systemd_user(EXE)):
        assert f"ConditionPathExists={EXE}" in text


def test_uninstall_without_a_service_says_so(as_linux, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", tmp_path / "none.service")
    assert cli.main(["service", "uninstall"]) == 0
    assert "not installed" in capsys.readouterr().out


def test_status_exit_codes(as_linux, monkeypatch, tmp_path, capsys):
    unit = tmp_path / "penguwatch.service"
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", unit)
    assert service.cmd_status(None, FakeRun()) == 3
    unit.write_text(service.render_systemd_system(EXE, {"PENGUWATCH_PORT": "9555"}))
    monkeypatch.setattr(service, "health_body", lambda host, port, timeout=3.0: None)
    run = FakeRun({("systemctl", "is-active"): (0, "active\n")})
    assert service.cmd_status(None, run) == 1
    out = capsys.readouterr().out
    assert "system service: running" in out and "nothing healthy answers on port 9555" in out
    seen = []
    monkeypatch.setattr(service, "health_body", lambda host, port, timeout=3.0: seen.append((host, port)) or {"status": "ok"})
    assert service.cmd_status(None, run) == 0
    assert seen == [("127.0.0.1", 9555)] and "answering on port 9555" in capsys.readouterr().out


def test_health_reads_the_health_endpoint(monkeypatch):
    class Reply:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    urls = []
    monkeypatch.setattr(service.urllib.request, "urlopen", lambda url, timeout: urls.append(url) or Reply(b'{"status":"ok"}'))
    assert service.health_body("0.0.0.0", 8080) == {"status": "ok"} and urls == ["http://127.0.0.1:8080/api/health"]
    assert service.health_body("::1", 8080) == {"status": "ok"} and urls[-1] == "http://[::1]:8080/api/health"
    monkeypatch.setattr(service.urllib.request, "urlopen", lambda url, timeout: Reply(b'{"status":"sick"}'))
    assert service.health_body("127.0.0.1", 8080) is None
    monkeypatch.setattr(service.urllib.request, "urlopen", lambda url, timeout: Reply(b"not json"))
    assert service.health_body("127.0.0.1", 8080) is None


# ---- quoting ----------------------------------------------------------------------------------------------------------------

def test_systemd_lines_quote_what_would_split_or_start_another_setting():
    assert service._sd_word("/opt/my app/penguwatch") == '"/opt/my app/penguwatch"'
    assert service._sd_word("50%") == '"50%%"'
    assert service._sd_word('a"b') == '"a\\"b"'
    assert service._exec_start("/opt/a b/pw") == 'ExecStart="/opt/a b/pw" serve'
    assert service._exec_start("/opt/$x/pw") == 'ExecStart="/opt/$$x/pw" serve'
    text = service.render_systemd_user("/opt/a b/pw", {"PENGUWATCH_HOST": "x y"})
    assert 'Environment="PENGUWATCH_HOST=x y"' in text


def test_plain_values_stay_unquoted_in_the_unit():
    assert service._sd_word("0.0.0.0") == "0.0.0.0"
    assert "Environment=PENGUWATCH_PORT=9000" in service.render_systemd_user(EXE, {"PENGUWATCH_PORT": "9000"})


@pytest.mark.parametrize("host", ["a\nExecStartPre=/bin/evil", "a\rb", "a\x00b"])
def test_a_control_character_in_host_is_refused_by_every_plan(host):
    for kind, scope in (("linux", SYSTEM), ("linux", USER), ("macos", SYSTEM), ("macos", USER), ("windows", USER)):
        with pytest.raises(service.ServiceError, match="control character"):
            service.plan_install(kind, scope, python="/x/bin/python" if kind != "windows" else r"C:\x\python.exe",
                                 host=host, account="a", config_dir=Path("/c"))


def test_a_path_with_a_newline_is_refused():
    with pytest.raises(service.ServiceError):
        service.plan_install("linux", SYSTEM, python="/opt/x\ny/bin/python")


def test_the_windows_task_arguments_quote_the_host():
    plan = service.plan_install("windows", USER, python=r"C:\p\python.exe", host='a b" --port 1', account="a",
                                config_dir=PureWindowsPath(r"C:\C"))
    arguments = ET.fromstring(plan.files["<task.xml>"].split("\n", 1)[1]).find("t:Actions/t:Exec/t:Arguments", NS).text
    assert arguments == 'serve --host "a b\\" --port 1"'


# ---- secrets never appear -------------------------------------------------------------------------------------------------------

def test_a_secret_in_the_environment_or_the_settings_file_is_in_no_definition_and_no_output(as_linux, monkeypatch, tmp_path, capsys):
    """The token lives in penguwatch.env (read by the program, or by systemd as EnvironmentFile) and possibly in the
    shell: no definition is rendered from it, and neither dry-run nor install nor status prints it."""
    monkeypatch.setenv("PENGUWATCH_ACCESS_TOKEN", SECRET)
    monkeypatch.setenv("PENGUWATCH_NOTIFY_URL", "https://ntfy.example/" + SECRET)
    env_file = tmp_path / ".config" / "penguwatch" / "penguwatch.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"PENGUWATCH_ACCESS_TOKEN={SECRET}\nPENGUWATCH_NOTIFY_URL=https://x/{SECRET}\n")
    rendered = []
    for kind, scope in (("linux", SYSTEM), ("linux", USER), ("macos", SYSTEM), ("macos", USER), ("windows", USER)):
        plan = service.plan_install(kind, scope, python="/x/bin/python" if kind != "windows" else r"C:\x\python.exe",
                                    host="0.0.0.0", port=9000, home=tmp_path, account="a", config_dir=env_file.parent)
        rendered += [*plan.files.values(), *plan.notes, *(s.describe() for s in plan.steps),
                     *(s.text or "" for s in plan.steps if s.kind == "write")]
    assert not any(SECRET in text for text in rendered)
    # a template is created only when there is none, and it is a template, not the content of any existing file
    assert SECRET not in service.envfile.TEMPLATE
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(["service", "install", "--user", "--dry-run", "--host", "0.0.0.0"]) == 0
    assert cli.main(["service", "uninstall", "--user", "--dry-run"]) == 0
    service.cmd_status(None, FakeRun())
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "access token you set protects it" not in captured.out  # (dry run prints no advice) ...
    assert service._token_is_set("linux", USER, tmp_path)           # ... but it knows a token exists, by name only


def test_a_wide_listen_address_gets_the_token_advice_and_a_set_token_stops_it(as_linux, monkeypatch, tmp_path):
    lines = service._other_devices_lines(8080, False)
    assert any("PENGUWATCH_ACCESS_TOKEN" in line and "anyone on the network" in line for line in lines)
    lines = service._other_devices_lines(8080, True)
    assert any("access token you set protects it" in line for line in lines) and not any("Set PENGUWATCH" in x for x in lines)
    assert not service._token_is_set("linux", USER, tmp_path)
    env_file = Path(service.settings_file_for("linux", USER))
    env_file.parent.mkdir(parents=True)
    env_file.write_text("PENGUWATCH_ACCESS_TOKEN=\n")   # the template line, no value: not set
    assert not service._token_is_set("linux", USER, tmp_path)
    env_file.write_text(f"PENGUWATCH_ACCESS_TOKEN={SECRET}\n")
    assert service._token_is_set("linux", USER, tmp_path)


def test_the_default_listen_address_is_one_constant_that_the_service_and_the_command_share():
    assert paths.DEFAULT_HOST == "127.0.0.1" and paths.DEFAULT_PORT == 8080
    assert paths.listen_address(None, {})[:2] == (paths.DEFAULT_HOST, paths.DEFAULT_PORT)
    assert paths.listen_address({"host": "192.0.2.2", "port": 9}, {})[:2] == ("192.0.2.2", 9)
    assert paths.listen_address({"host": "192.0.2.2"}, {"PENGUWATCH_HOST": "h", "PENGUWATCH_PORT": "7"})[:2] == ("h", 7)
    with pytest.raises(ValueError, match="PENGUWATCH_PORT"):
        paths.listen_address(None, {"PENGUWATCH_PORT": "x"})
    # nobody else spells the default out
    for path in (ROOT / "src" / "penguwatch").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "paths.py":
            assert 'DEFAULT_HOST = "' not in text, path
