"""penguwatch.env: parsed strictly, loaded without overriding the environment, and never echoing a value."""

import pytest

from penguwatch import cli, envfile, paths

SECRET = "tok-4f3e2d1c0b"


def test_lines_comments_quotes_and_export():
    values, problems = envfile.parse(
        "# comment\n\nPENGUWATCH_PORT=9000\nexport PENGUWATCH_HOST = 0.0.0.0 \nA='single quoted'\nB=\"double\"\nEMPTY=\nC=a=b\n")
    assert values == {"PENGUWATCH_PORT": "9000", "PENGUWATCH_HOST": "0.0.0.0", "A": "single quoted", "B": "double",
                      "EMPTY": "", "C": "a=b"}
    assert problems == []


def test_bad_lines_are_skipped_and_named_by_number_never_by_value():
    values, problems = envfile.parse(f"not a setting {SECRET}\n1BAD={SECRET}\nOK=1\nPATH=/evil\nLD_PRELOAD=/x.so\nPYTHONPATH=/x\n")
    assert values == {"OK": "1"}
    assert [p.split(":")[0] for p in problems] == ["line 1", "line 2", "line 4", "line 5", "line 6"]
    assert SECRET not in " ".join(problems)


def test_load_never_overrides_what_is_already_set_and_missing_or_unreadable_files_are_fine(tmp_path):
    f = tmp_path / "penguwatch.env"
    f.write_text("PENGUWATCH_PORT=9000\nPENGUWATCH_NOTIFY_URL=https://x\n")
    environ = {"PENGUWATCH_PORT": "7"}
    assert envfile.load(f, environ) == []
    assert environ == {"PENGUWATCH_PORT": "7", "PENGUWATCH_NOTIFY_URL": "https://x"}
    assert envfile.load(tmp_path / "nope.env", environ) == []
    envfile.load(tmp_path, environ)  # a folder in its place: reported or ignored, never a crash


def test_the_template_is_all_comments_so_a_fresh_install_changes_nothing():
    values, problems = envfile.parse(envfile.TEMPLATE)
    assert values == {} and problems == []


def test_the_file_sits_next_to_the_config_file_unless_told_otherwise(tmp_path):
    assert paths.env_file_path({"PENGUWATCH_CONFIG": str(tmp_path / "c" / "config.yaml")}) == tmp_path / "c" / "penguwatch.env"
    assert paths.env_file_path({"PENGUWATCH_ENV_FILE": str(tmp_path / "x.env")}) == tmp_path / "x.env"
    assert paths.env_file_path({"XDG_CONFIG_HOME": str(tmp_path)}, "linux") == tmp_path / "penguwatch" / "penguwatch.env"


def test_serve_loads_it_before_it_decides_host_and_port_and_the_token_reaches_the_app(tmp_path, monkeypatch, capsys):
    import os

    import penguwatch.main as app_main
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT",
                 "PENGUWATCH_ACCESS_TOKEN", "PENGUWATCH_ENV_FILE"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    config = tmp_path / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text("server: {port: 9100}\n")
    (config.parent / "penguwatch.env").write_text(f"PENGUWATCH_ACCESS_TOKEN={SECRET}\nPENGUWATCH_PORT=9555\nBROKEN LINE\n")
    seen = {}
    monkeypatch.setattr(app_main, "main", lambda: seen.update(token=os.environ.get("PENGUWATCH_ACCESS_TOKEN"),
                                                                port=os.environ["PENGUWATCH_PORT"]))
    assert cli.main(["serve", "--config", str(config), "--data-dir", str(tmp_path / "d")]) == 0
    assert seen == {"token": SECRET, "port": "9555"}   # the variable from the file beats server.port in the config
    captured = capsys.readouterr()
    assert "line 3" in captured.err and SECRET not in captured.out + captured.err
    for name in ("PENGUWATCH_CONFIG", "PENGUWATCH_DATA", "PENGUWATCH_DB_PATH", "PENGUWATCH_HOST", "PENGUWATCH_PORT",
                 "PENGUWATCH_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_paths_shows_where_the_settings_file_is_but_not_what_is_in_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PENGUWATCH_CONFIG", str(tmp_path / "config.yaml"))
    (tmp_path / "penguwatch.env").write_text(f"PENGUWATCH_ACCESS_TOKEN={SECRET}\n")
    assert cli.main(["paths"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "penguwatch.env") in out and SECRET not in out


@pytest.mark.parametrize("text", ["A=\x00b\n"])
def test_a_nul_in_a_value_is_refused(text):
    assert envfile.parse(text)[0] == {}
