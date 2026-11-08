"""`penguwatch backup` (a consistent copy of the database and the sessions file) and `penguwatch doctor`."""

import socket
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from penguwatch import cli, doctor, paths

ROOT = Path(__file__).resolve().parents[1]
POSIX = sys.platform != "win32"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT",
                 "PENGUWATCH_ENV_FILE", "PENGUWATCH_TZ", "TZ", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PENGUWATCH_CONFIG", str(tmp_path / "config.yaml"))


def make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY, title TEXT)")
    con.executemany("INSERT INTO shows (title) VALUES (?)", [(f"show {i}",) for i in range(rows)])
    con.commit()
    con.close()


# ---- backup ------------------------------------------------------------------------------------------------------------

def test_backup_copies_the_database_and_the_sessions_file_into_a_timestamped_folder(tmp_path, capsys):
    data = tmp_path / "data"
    make_db(data / "penguwatch.db")
    (data / "sessions.json").write_text('{"revoked": []}')
    assert cli.main(["backup", "--data-dir", str(data)]) == 0
    folder = Path(capsys.readouterr().out.strip())
    assert folder.parent == data / "backups" and len(folder.name) == len("20261104T120000Z")
    assert sorted(p.name for p in folder.iterdir()) == ["penguwatch.db", "sessions.json"]
    copy = sqlite3.connect(folder / "penguwatch.db")
    assert copy.execute("SELECT count(*) FROM shows").fetchone() == (3,)
    copy.close()
    assert (folder / "sessions.json").read_text() == '{"revoked": []}'
    if POSIX:
        assert folder.stat().st_mode & 0o777 == 0o700
        assert (folder / "penguwatch.db").stat().st_mode & 0o777 == 0o600


def test_backup_copies_secrets_json_private(tmp_path, capsys):
    data = tmp_path / "data"
    make_db(data / "penguwatch.db")
    (data / "secrets.json").write_text('{"version": 1, "notify": {"url": "https://ntfy.example.com/t"}}')
    assert cli.main(["backup", "--data-dir", str(data)]) == 0
    folder = Path(capsys.readouterr().out.strip())
    assert sorted(p.name for p in folder.iterdir()) == ["penguwatch.db", "secrets.json"]
    assert (folder / "secrets.json").read_text() == (data / "secrets.json").read_text()
    if POSIX:
        assert (folder / "secrets.json").stat().st_mode & 0o777 == 0o600


def test_backup_is_consistent_while_the_database_is_being_written(tmp_path):
    db = tmp_path / "d" / "penguwatch.db"
    make_db(db, rows=50)
    writer = sqlite3.connect(db)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO shows (title) VALUES ('uncommitted')")
    folder = cli.make_backup(db, now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    writer.rollback()
    writer.close()
    assert folder.name == "20260102T030405Z"
    copy = sqlite3.connect(folder / "penguwatch.db")
    assert copy.execute("SELECT count(*) FROM shows").fetchone() == (50,)  # a snapshot: the open transaction is not in it
    assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    copy.close()


def test_backup_without_a_database_says_no_database(tmp_path):
    with pytest.raises(SystemExit, match="no database"):
        cli.make_backup(tmp_path / "d" / "penguwatch.db")


def test_backup_to_a_chosen_folder_and_twice_in_one_second_is_refused(tmp_path):
    db = tmp_path / "d" / "penguwatch.db"
    make_db(db)
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    first = cli.make_backup(db, str(tmp_path / "out"), now)
    assert first == tmp_path / "out" / "20260102T030405Z"
    with pytest.raises(SystemExit, match="already exists"):
        cli.make_backup(db, str(tmp_path / "out"), now)


@pytest.mark.skipif(not POSIX, reason="symbolic links need POSIX")
def test_backup_refuses_a_sessions_file_that_is_a_symbolic_link_and_leaves_no_half_backup(tmp_path):
    db = tmp_path / "d" / "penguwatch.db"
    make_db(db)
    (tmp_path / "other-secret").write_text("do not copy me")
    (db.parent / "sessions.json").symlink_to(tmp_path / "other-secret")
    with pytest.raises(SystemExit, match="symbolic link"):
        cli.make_backup(db)
    assert list((db.parent / "backups").iterdir()) == []


def test_backup_follows_the_database_path_of_the_config_and_the_environment(tmp_path, monkeypatch, capsys):
    db = tmp_path / "elsewhere" / "x.db"
    make_db(db)
    (tmp_path / "config.yaml").write_text(f"database: {{db_path: {db}}}\n")
    assert cli.main(["backup"]) == 0
    assert Path(capsys.readouterr().out.strip()).parent == db.parent / "backups"


def test_system_backup_needs_a_target_and_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.service, "system_kind", lambda platform=None: "linux")
    with pytest.raises(SystemExit, match="say where the copy goes"):
        cli.main(["backup", "--system"])
    if hasattr(cli.os, "geteuid"):
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemExit, match="run this with sudo"):
            cli.main(["backup", "--system", "--to", "/tmp/x"])


def test_system_flag_points_every_lookup_at_the_services_files(monkeypatch, capsys):
    monkeypatch.setattr(cli.service, "system_kind", lambda platform=None: "linux")
    monkeypatch.setattr(cli.service, "SYSTEM_UNIT_PATH", Path("/nonexistent/penguwatch.service"))
    assert cli.main(["paths", "--system"]) == 0
    out = capsys.readouterr().out
    assert "/var/lib/penguwatch" in out and "/etc/penguwatch/config.yaml" in out
    for name in ("PENGUWATCH_DATA", "PENGUWATCH_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def test_paths_says_where_the_settings_are(tmp_path, capsys):
    data = tmp_path / "data"
    assert cli.main(["paths", "--data-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "env file" in out and "settings file" not in out
    assert "not imported yet" in out
    _imported_db(data / "penguwatch.db")
    assert cli.main(["paths", "--data-dir", str(data)]) == 0
    assert "in the database (since 2026-11-07)" in capsys.readouterr().out


# ---- doctor ------------------------------------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_doctor(argv, health=lambda host, port: None, services=None):
    import io

    out = io.StringIO()
    args = cli.build_parser().parse_args(["doctor", *argv])
    code = doctor.run(cli.resolve(args), offline=args.offline, out=out, health=health,
                      services=services or (lambda: [(doctor.OK, "SIMKL", "reachable")]), env={})
    return code, out.getvalue()


def test_doctor_all_good(tmp_path):
    (tmp_path / "config.yaml").write_text("server: {port: 8080}\n")
    code, out = run_doctor(["--data-dir", str(tmp_path / "d"), "--port", str(free_port())])
    assert code == 0, out
    assert "config file" in out and "data folder" in out and "will be created" in out and "is free" in out
    assert out.strip().endswith("Nothing is wrong.")


def test_doctor_port_free_in_use_by_penguwatch_or_by_something_else(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(port)])
        assert code == 1 and "FAIL  port" in out and "pick another with --port" in out
        code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(port)], health=lambda h, p: {"status": "ok"})
        assert code == 0 and "served by PenguWatch already" in out


def test_doctor_config_problems(tmp_path):
    code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(free_port())])
    assert code == 0 and "warn  config file" in out and "does not exist" in out   # missing: the example is used
    bad = tmp_path / "config.yaml"
    bad.write_text("server: [unclosed\n")
    code, out = doctor.check_config(bad, True)[0], doctor.check_config(bad, True)[2]
    assert code == doctor.FAIL and "not valid YAML" in out
    bad.write_text("- a\n- b\n")
    assert doctor.check_config(bad, True)[0] == doctor.FAIL
    bad.write_bytes(b"\xff\xfe\x00bad")
    assert doctor.check_config(bad, True)[0] == doctor.FAIL


@pytest.mark.skipif(not POSIX or (hasattr(sys.modules["os"], "geteuid") and sys.modules["os"].geteuid() == 0),
                    reason="needs a user who can be refused (not root)")
def test_doctor_data_folder_not_writable(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert doctor.check_data_dir(locked)[0] == doctor.FAIL
        assert doctor.check_data_dir(locked / "new")[0] == doctor.FAIL
    finally:
        locked.chmod(0o700)


def test_doctor_time_zone():
    assert doctor.check_time_zone(None, None)[0] == doctor.OK
    assert doctor.check_time_zone("Europe/Berlin", None)[:2] == (doctor.OK, "time zone")
    assert doctor.check_time_zone(None, "UTC")[2] == "UTC"
    level, _, detail = doctor.check_time_zone("Mars/Olympus", None)
    assert level == doctor.FAIL and "tzdata" in detail


def test_doctor_reads_the_time_zone_from_the_config_and_the_environment(tmp_path):
    (tmp_path / "config.yaml").write_text("server: {timezone: Mars/Olympus}\n")
    code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(free_port())])
    assert code == 1 and "Mars/Olympus" in out


def test_doctor_service_unreachable_is_a_warning_and_offline_skips_it(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("{}\n")
    code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(free_port())],
                           services=lambda: [(doctor.WARN, "SIMKL", "not reachable")])
    assert code == 0 and "warn  SIMKL" in out
    code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(free_port()), "--offline"],
                           services=lambda: pytest.fail("offline must not call the services"))
    assert code == 0 and "skipped (--offline)" in out

    def refuse(*a, **k):
        raise doctor.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(doctor.urllib.request, "urlopen", refuse)
    assert [row[:2] for row in doctor.check_services()] == [(doctor.WARN, "SIMKL"), (doctor.WARN, "TVmaze")]  # every driver with a service
    monkeypatch.setattr(doctor.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        doctor.urllib.error.HTTPError("u", 404, "nf", {}, None)))
    assert doctor.check_services() == [(doctor.OK, "SIMKL", "api.simkl.com is reachable"),
                                      (doctor.OK, "TVmaze", "api.tvmaze.com is reachable")]  # any HTTP answer will do


def _imported_db(path: Path, zone: str = "Asia/Tokyo") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE settings_values (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)")
    con.executemany("INSERT INTO app_state VALUES (?, ?)", [("settings_initialized", "1"), ("settings_revision", "4"),
                                                            ("settings_imported_at", "1794073414.0")])
    con.executemany("INSERT INTO settings_values VALUES (?, ?, 0)", [("server.timezone", f'"{zone}"'),
                                                                    ("mcp.enabled", "true")])
    con.commit()
    con.close()


def test_doctor_reads_the_stored_settings_and_names_the_differing_keys(tmp_path):
    (tmp_path / "config.yaml").write_text("server: {timezone: Europe/Paris}\nmcp: {enabled: false}\n")
    db = tmp_path / "penguwatch.db"
    _imported_db(db)
    before = db.read_bytes()
    code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(free_port())])
    assert code == 0, out  # the stored zone is the one in use: config.yaml's is ignored
    assert "ok  time zone    Asia/Tokyo" in out
    assert "in the database since 2026-11-07, revision 4" in out
    assert "2 keys differ: mcp.enabled, server.timezone" in out and "Paris" not in out
    assert db.read_bytes() == before and not (tmp_path / "penguwatch.db-wal").exists()


def test_doctor_warns_about_an_ignored_variable_without_its_value(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    args = cli.build_parser().parse_args(["doctor", "--data-dir", str(tmp_path), "--port", str(free_port())])
    import io
    out = io.StringIO()
    code = doctor.run(cli.resolve(args), out=out, health=lambda h, p: None, services=lambda: [],
                      env={"PENGUWATCH_NOTIFY_HOUR": "25", "PENGUWATCH_MCP_ENABLED": "maybe-xyz"})
    text = out.getvalue()
    assert code == 0 and "warn  variable" in text
    assert "PENGUWATCH_NOTIFY_HOUR is set but not valid" in text and "PENGUWATCH_MCP_ENABLED is set but not valid" in text
    assert "maybe-xyz" not in text and " 25" not in text


def test_doctor_names_no_setting_values(tmp_path, monkeypatch):
    secret = "tok-should-not-show-1a2b"
    (tmp_path / "config.yaml").write_text(f"simkl: {{client_id: {secret}, access_token: {secret}}}\n")
    monkeypatch.setenv("PENGUWATCH_ACCESS_TOKEN", secret)
    code, out = run_doctor(["--data-dir", str(tmp_path), "--port", str(free_port())])
    assert code == 0 and secret not in out


# ---- the entry points --------------------------------------------------------------------------------------------------

def test_the_package_declares_the_console_and_the_windowless_command():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["penguwatch"] == "penguwatch.cli:main"
    assert project["gui-scripts"]["penguwatchw"] == "penguwatch.cli:main"
    assert 'tzdata>=2024.1; sys_platform == "win32"' in project["dependencies"]
    assert 'tzdata>=2024.1; sys_platform == "win32"' in (ROOT / "requirements.in").read_text(encoding="utf-8")


def test_without_a_console_the_streams_go_to_a_log_file_in_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._quiet_streams(tmp_path / "state")
    print("hello log")
    sys.stderr.flush()
    stream = sys.stdout
    assert (tmp_path / "state" / "penguwatch.log").read_text(encoding="utf-8") == "hello log\n"
    stream.close()


def test_the_log_file_is_rotated_when_it_is_big(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "penguwatch.log").write_text("x" * (cli.LOG_ROTATE_BYTES + 1))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._quiet_streams(state)
    sys.stdout.close()
    assert (state / "penguwatch.log.1").stat().st_size == cli.LOG_ROTATE_BYTES + 1
    assert (state / "penguwatch.log").stat().st_size == 0


def test_with_a_console_nothing_is_replaced(monkeypatch, tmp_path):
    before = sys.stdout, sys.stderr
    cli._quiet_streams(tmp_path / "state")
    assert (sys.stdout, sys.stderr) == before and not (tmp_path / "state").exists()


def test_the_command_line_has_every_new_command():
    parser = cli.build_parser()
    for argv in (["backup", "--to", "x"], ["backup", "--system", "--to", "x"], ["doctor", "--offline"], ["paths", "--system"],
                 ["settings", "check-import", "c.yaml", "d.db"], ["settings", "check-import", "c.yaml", "d.db", "--live"],
                 ["service", "install", "--user", "--host", "0.0.0.0", "--port", "9000", "--dry-run"],
                 ["service", "uninstall", "--system", "--dry-run"], ["service", "status"]):
        parser.parse_args(argv)
    with pytest.raises(SystemExit):
        parser.parse_args(["service"])   # a subcommand is required


def test_a_bad_port_in_the_environment_is_one_line_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("PENGUWATCH_PORT", "eighty")
    monkeypatch.setattr(cli.service, "system_kind", lambda platform=None: "linux")
    monkeypatch.setattr(cli.service, "installed_scopes", lambda *a, **k: [cli.service.SYSTEM])
    monkeypatch.setattr(cli.service, "manager_state", lambda *a, **k: "running")
    assert cli.main(["service", "status"]) == 2
    assert "PENGUWATCH_PORT must be a number" in capsys.readouterr().err
