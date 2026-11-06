"""The `penguwatch` command: where it looks for files and what it tells the app."""

from pathlib import Path

import pytest

from penguwatch import __version__, cli, paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def test_version(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"penguwatch {__version__}"


def test_default_places_per_system(tmp_path):
    home = tmp_path
    assert paths.default_data_dir({}, "linux") == home / ".local" / "share" / "penguwatch"
    assert paths.default_config_dir({}, "linux") == home / ".config" / "penguwatch"
    assert paths.default_data_dir({"XDG_DATA_HOME": str(home / "xd")}, "linux") == home / "xd" / "penguwatch"
    assert paths.default_config_dir({"XDG_CONFIG_HOME": "relative"}, "linux") == home / ".config" / "penguwatch"
    assert paths.default_data_dir({}, "darwin") == home / "Library" / "Application Support" / "PenguWatch"
    assert paths.default_config_dir({}, "darwin") == home / "Library" / "Application Support" / "PenguWatch"
    assert paths.default_data_dir({"LOCALAPPDATA": "C:/L"}, "win32") == Path("C:/L") / "PenguWatch"
    assert paths.default_config_dir({"APPDATA": "C:/R"}, "win32") == Path("C:/R") / "PenguWatch"


def test_database_path_rules():
    assert paths.db_path(None, {"PENGUWATCH_DATA": "/d"}) == str(Path("/d") / "penguwatch.db")
    assert paths.db_path("data/penguwatch.db", {"PENGUWATCH_DATA": "/d"}) == str(Path("/d") / "penguwatch.db")  # legacy
    assert paths.db_path("/elsewhere/x.db", {"PENGUWATCH_DATA": "/d"}) == "/elsewhere/x.db"
    assert paths.db_path("/elsewhere/x.db", {"PENGUWATCH_DB_PATH": "/env.db"}) == "/env.db"


def parse(*argv):
    return cli.build_parser().parse_args(["paths", *argv])


def test_defaults_listen_on_this_computer_only(tmp_path):
    settings = cli.resolve(parse())
    assert settings["host"] == ("127.0.0.1", "default")
    assert settings["port"] == (8080, "default")
    assert settings["db"][0] == str(tmp_path / ".local" / "share" / "penguwatch" / "penguwatch.db")
    assert settings["config_exists"][0] is False


def test_options_beat_environment_beat_config_file(tmp_path, monkeypatch):
    config = tmp_path / "c.yaml"
    config.write_text("server: {host: 192.0.2.5, port: 9100}\ndatabase: {db_path: /cfg/x.db}\n", encoding="utf-8")
    settings = cli.resolve(parse("--config", str(config)))
    assert settings["host"][0] == "192.0.2.5" and settings["port"][0] == 9100 and settings["db"][0] == "/cfg/x.db"
    monkeypatch.setenv("PENGUWATCH_PORT", "9200")
    monkeypatch.setenv("PENGUWATCH_HOST", "0.0.0.0")
    settings = cli.resolve(parse("--config", str(config)))
    assert settings["host"] == ("0.0.0.0", "PENGUWATCH_HOST") and settings["port"] == ("9200", "PENGUWATCH_PORT")
    settings = cli.resolve(parse("--config", str(config), "--port", "9300", "--host", "::1", "--data-dir", str(tmp_path)))
    assert settings["host"] == ("::1", "--host") and settings["port"] == (9300, "--port")
    assert settings["data"][0] == str(tmp_path)


def test_the_old_example_config_does_not_pin_the_database_to_the_working_directory(tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("database:\n  db_path: data/penguwatch.db\n", encoding="utf-8")
    settings = cli.resolve(parse("--config", str(config), "--data-dir", str(tmp_path / "d")))
    assert settings["db"][0] == str(tmp_path / "d" / "penguwatch.db")


def test_init_config_writes_the_example_once(tmp_path, capsys):
    target = tmp_path / "out" / "config.yaml"
    assert cli.main(["init-config", "--config", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == (Path(cli.__file__).parent / "config.example.yaml").read_text(encoding="utf-8")
    assert cli.main(["init-config", "--config", str(target)]) == 1  # does not overwrite
    assert "already exists" in capsys.readouterr().err
    target.write_text("x", encoding="utf-8")
    assert cli.main(["init-config", "--config", str(target), "--force"]) == 0
    assert target.read_text(encoding="utf-8") != "x"


def test_serve_sets_the_variables_the_app_reads_and_starts_it(tmp_path, monkeypatch):
    started = []
    import penguwatch.main as app_main
    monkeypatch.setattr(app_main, "main", lambda: started.append(dict(
        config=__import__("os").environ["PENGUWATCH_CONFIG"], db=__import__("os").environ["PENGUWATCH_DB_PATH"])))
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT"):
        monkeypatch.setenv(name, "")  # restored afterwards; cmd_serve sets them for real
        monkeypatch.delenv(name)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "d"), "--port", "9400"]) == 0
    assert started == [{"config": str(cli.EXAMPLE_CONFIG), "db": str(tmp_path / "d" / "penguwatch.db")}]
    assert (tmp_path / "d").is_dir()
    import os
    assert os.environ["PENGUWATCH_PORT"] == "9400" and os.environ["PENGUWATCH_HOST"] == "127.0.0.1"
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT"):
        monkeypatch.delenv(name, raising=False)
