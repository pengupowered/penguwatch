"""scripts/install.sh, scripts/install.ps1 and scripts/release-assets.sh: what can be checked without installing
anything (the install jobs of ci.yml run the scripts for real on Linux, macOS and Windows). The tests run the shell
scripts as far as they go offline: they must refuse a wheel or lock that does not match SHA256SUMS before they
download or change anything."""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from penguwatch import paths, service

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
PS1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
RELEASE_ASSETS = ROOT / "scripts" / "release-assets.sh"
VERSION = "9.8.7"
posix_shell = pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="needs a POSIX shell")


def _sh_pins() -> dict:
    return {m.group(1).lower().replace("_", "-"): m.group(2)
            for m in re.finditer(r'^UV_SHA256_([A-Z0-9_]+)="([0-9a-f]+)"$', SH, re.M)}


def _ps1_pins() -> dict:
    block = re.search(r"\$UvSha256 = @\{(.*?)\n\}", PS1, re.S).group(1)
    return dict(re.findall(r"'([\w-]+)'\s*=\s*'([0-9a-f]+)'", block))


def test_both_scripts_pin_the_same_uv_and_python():
    assert re.search(r'^UV_VERSION="([\d.]+)"$', SH, re.M).group(1) == re.search(r"\$UvVersion = '([\d.]+)'", PS1).group(1)
    assert re.search(r'^PYTHON_VERSION="([\d.]+)"$', SH, re.M).group(1) == re.search(r"\$PythonVersion = '([\d.]+)'", PS1).group(1) == "3.12"


def test_every_uv_archive_has_a_sha256_pin():
    sh, ps1 = _sh_pins(), _ps1_pins()
    assert set(sh) == {"x86-64-unknown-linux-musl", "aarch64-unknown-linux-musl", "aarch64-apple-darwin", "x86-64-apple-darwin"}
    assert set(ps1) == {"x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"}
    for digest in [*sh.values(), *ps1.values()]:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert len(set([*sh.values(), *ps1.values()])) == 6  # six different archives, no digest copied twice
    for target in ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl", "aarch64-apple-darwin", "x86_64-apple-darwin"):
        assert f'UV_TARGET="{target}"' in SH


def test_the_pinned_uv_archive_is_what_is_downloaded_and_checked_before_it_is_unpacked():
    assert 'releases/download/$UV_VERSION/$UV_ARCHIVE' in SH
    check = SH.index('[ "$(sha256_of "$WORK/$UV_ARCHIVE")" = "$UV_SHA256" ] || die')
    assert check < SH.index('tar -xzf "$WORK/$UV_ARCHIVE"')
    assert "does not match the sha256 pinned in this script" in SH and "does not match the sha256 pinned in this script" in PS1
    assert PS1.index("-ne $UvSha256[$UvTarget]") < PS1.index("Expand-Archive")


@pytest.mark.parametrize("script", [SH, PS1], ids=["install.sh", "install.ps1"])
def test_both_scripts_install_from_the_lock_with_hashes_and_wheels_only_then_the_wheel_then_the_service(script):
    assert "--require-hashes" in script and ":all:" in script and "--only-binary" in script
    assert "--no-deps" in script
    assert script.index("--require-hashes") < script.index("service', 'install'" if script is PS1 else "service install")
    assert "SHA256SUMS" in script and "backup" in script and "service" in script and "uninstall" in script
    assert "penguwatch-$VERSION.lock" in script.replace("$Version", "$VERSION")
    assert "py3-none-any.whl" in script


@pytest.mark.parametrize("script", [SH, PS1], ids=["install.sh", "install.ps1"])
def test_the_wheel_is_checked_again_right_before_it_is_installed(script):
    again = [m.start() for m in re.finditer(r'verify "\$WHEEL"|Confirm-Sha256 \$Wheel', script)]
    assert len(again) == 2   # once after the download, once right before `pip install` of the wheel
    install_wheel = max(m.start() for m in re.finditer(r"'install', '--python'|pip install --python", script))
    assert again[1] < install_wheel


def test_the_default_port_in_both_scripts_is_the_programs():
    assert re.search(r"^DEFAULT_PORT=(\d+)", SH, re.M).group(1) == str(paths.DEFAULT_PORT)
    assert re.search(r"^\$DefaultPort = (\d+)", PS1, re.M).group(1) == str(paths.DEFAULT_PORT)
    assert "127.0.0.1" not in SH.split("# ---- service", 1)[0].replace("127.*", "")  # the default host is the program's, not spelled here


def test_the_release_assets_script_names_the_files_the_installers_look_for():
    text = RELEASE_ASSETS.read_text(encoding="utf-8")
    assert "penguwatch-$version.lock" in text and "install.sh" in text and "install.ps1" in text and "SHA256SUMS" in text


def test_install_sh_uses_the_directories_that_the_service_command_does():
    assert f'APP="{service.LINUX_APP_DIR}"; DATA="{service.SYSTEM_DATA_DIR}"' in SH
    assert f'APP="{service.MAC_APP_DIR}"; DATA="{service.MAC_DAEMON_DATA_DIR}"' in SH
    assert f'SYSTEM_CONFIG="{service.SYSTEM_CONFIG_DIR}/config.yaml"' in SH
    assert f'sudo -u {service.MAC_USER}' in SH
    assert f"com.pengupowered.penguwatch" in SH and service.LABEL == "com.pengupowered.penguwatch"


def test_a_user_install_keeps_the_program_in_a_folder_of_its_own_inside_the_data_folder():
    """The default data folder of the `penguwatch` command is ~/.local/share/penguwatch: the program must not be that
    folder, or `--uninstall` (rm -rf of the program) would delete the database."""
    assert 'DATA="${XDG_DATA_HOME:-$HOME/.local/share}/penguwatch"; APP="$DATA/app"' in SH
    assert str(paths.default_data_dir({"XDG_DATA_HOME": "/x"}, "linux")) == "/x/penguwatch"
    assert 'APP="$HOME/Library/Application Support/PenguWatch/app"; DATA="$HOME/Library/Application Support/PenguWatch"' in SH
    assert str(paths.default_data_dir({}, "darwin")).endswith("Library/Application Support/PenguWatch")
    assert "Programs\\PenguWatch'" in PS1 and "$Data = Join-Path $env:LOCALAPPDATA 'PenguWatch'" in PS1


def test_an_upgrade_stops_the_service_so_its_listen_address_survives_and_uninstall_removes_the_token_file():
    upgrade = SH.split("# ---- upgrade: back up, stop", 1)[1].split("# ---- install", 1)[0]
    assert "stop_service" in upgrade and "service uninstall" not in upgrade
    upgrade_ps1 = PS1.split("# ---- upgrade: back up, stop", 1)[1].split("# ---- install", 1)[0]
    assert "schtasks /End" in upgrade_ps1 and "service uninstall" not in upgrade_ps1
    uninstall = SH.split("# ---- uninstall", 1)[1].split("# ---- tools", 1)[0]
    assert "penguwatch.service.d" in uninstall  # the drop-in folder that may hold PENGUWATCH_ACCESS_TOKEN


def test_the_scripts_take_host_and_port_and_warn_about_listening_beyond_this_computer():
    for token in ('--host) ', '--port) ', "PENGUWATCH_ACCESS_TOKEN"):
        assert token in SH
    assert "[Alias('Host')][string]$ListenHost" in PS1 and "[int]$Port" in PS1 and "PENGUWATCH_ACCESS_TOKEN" in PS1
    assert '--host "$HOST"' in SH and "'--host', $ListenHost" in PS1


def test_the_scripts_never_print_the_settings_file():
    for script in (SH, PS1):
        for line in script.splitlines():
            if "penguwatch.env" in line and not line.lstrip().startswith("#"):
                assert not re.search(r"\b(cat|type|Get-Content|echo)\b", line), line


def test_the_locks_are_universal_and_carry_tzdata_for_windows():
    header = (ROOT / "requirements.txt").read_text(encoding="utf-8").split("aiosqlite", 1)[0]
    assert "# platform: universal\n" in header
    assert re.search(r"^tzdata==[\d.]+ ; sys_platform == 'win32' \\", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M)


def test_ps1_stays_ascii_so_windows_powershell_5_reads_it_without_a_byte_order_mark():
    PS1.encode("ascii")


def test_ps1_is_balanced_when_no_powershell_is_around():
    """A cheap parse check where pwsh is not available (CI parses it with Windows PowerShell for real)."""
    code = re.sub(r"#[^\n]*", "", re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "", PS1))
    for opening, closing in ("{}", "()", "[]"):
        assert code.count(opening) == code.count(closing), opening


# ---- install.sh, offline -----------------------------------------------------------------------------------------------------

@pytest.fixture
def assets(tmp_path):
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / f"penguwatch-{VERSION}-py3-none-any.whl").write_bytes(b"wheel")
    (folder / f"penguwatch-{VERSION}.lock").write_text("a==1 --hash=sha256:00\n")
    sums = "".join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in sorted(p.name for p in folder.iterdir()))
    (folder / "SHA256SUMS").write_text(sums)
    return folder


def _install(*args, env=None, home=None):
    # a user install, unless the tests run as root (a container), which only installs for the system; either way the
    # runs below stop before they change anything
    scope = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["--user"]
    environ = {**os.environ, **(env or {})}
    if home:
        environ["HOME"] = str(home)
        environ.pop("XDG_DATA_HOME", None)
    return subprocess.run(["sh", str(ROOT / "scripts" / "install.sh"), *scope, "--yes", "--no-browser", *args],
                          capture_output=True, text=True, check=False, cwd=ROOT, timeout=60, env=environ)


@posix_shell
def test_help_and_syntax():
    assert subprocess.run(["sh", "-n", str(ROOT / "scripts" / "install.sh")], check=False).returncode == 0
    done = _install("--help")
    assert done.returncode == 0 and "--from DIR" in done.stdout and "--host ADDR" in done.stdout


@posix_shell
def test_unknown_options_and_bad_values_are_refused():
    assert _install("--bogus").returncode == 1
    done = _install("--port", "eighty")
    assert done.returncode == 1 and "--port must be a number" in done.stderr
    done = _install("--host")
    assert done.returncode == 1 and "--host needs an address" in done.stderr
    done = _install("--host", "a\nb")
    assert done.returncode == 1 and "must not contain a newline" in done.stderr
    done = _install("--version", "latest-ish", "--from", ".")
    assert done.returncode == 1


@posix_shell
@pytest.mark.parametrize("version", ["1.2.3.4", "1.2", "1.2.3x", "1..3", ".1.2", "1.2.3.", "1.2.3\n9.9.9", "1.2.3 ", "1.2.3-rc1"])
def test_the_version_is_anchored_like_install_ps1(assets, version):
    done = _install("--version", version, "--from", str(assets))
    assert done.returncode == 1 and "not a version number" in done.stderr, done.stderr


@posix_shell
def test_a_newline_in_a_path_is_refused(tmp_path):
    done = _install("--from", str(tmp_path) + "\nX")
    assert done.returncode == 1 and "must not contain a newline" in done.stderr


def test_an_upgrade_backs_up_as_the_services_account_never_as_root():
    body = SH[SH.index("backup_data() {"):SH.index("confirm() {")]
    assert "systemd-run" in body and "DynamicUser=yes" in body and "User=penguwatch" in body
    assert "StateDirectory=penguwatch" in body and "--wait --pipe" in body
    assert 'PENGUWATCH_CONFIG="$SYSTEM_CONFIG"' in body
    assert "sudo -u _penguwatch" in body
    assert 'out="$(backup_data 2>&1)"' in SH
    assert 'root "$PENGUWATCH" backup' not in SH


@posix_shell
@pytest.mark.parametrize("victim", [f"penguwatch-{VERSION}-py3-none-any.whl", f"penguwatch-{VERSION}.lock"])
def test_a_file_that_does_not_match_sha256sums_stops_the_install_before_anything_is_downloaded(assets, victim):
    (assets / victim).write_bytes(b"tampered")
    done = _install("--from", str(assets))
    assert done.returncode == 1
    assert f"{victim} does not match SHA256SUMS" in done.stderr and "nothing was installed" in done.stderr
    assert done.stdout.splitlines() == [f"PenguWatch {VERSION}"]  # the version came from the wheel's name; then it stopped
    assert "uv" not in done.stdout.lower().replace("penguwatch", "")  # it never got as far as fetching uv


@posix_shell
@pytest.mark.skipif(sys.platform == "win32", reason="a fake curl is a POSIX script")
def test_a_uv_archive_that_does_not_match_the_pinned_sha256_is_refused_before_it_is_unpacked(assets, tmp_path):
    """The release files are fine; the "uv" the (fake) network returns is not the pinned one. Nothing may be unpacked,
    installed or run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "tar-ran"
    fake_curl = bin_dir / "curl"
    fake_curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; esac; shift; done\n'
                         'echo "not the pinned uv" > "$out"\n')
    fake_tar = bin_dir / "tar"
    fake_tar.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    for tool in (fake_curl, fake_tar):
        tool.chmod(0o755)
    done = _install("--from", str(assets), env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert done.returncode == 1
    assert "does not match the sha256 pinned in this script; nothing was installed" in done.stderr
    assert not marker.exists() and "uv " + re.search(r'^UV_VERSION="([\d.]+)"', SH, re.M).group(1) + " checked" not in done.stdout


@posix_shell
def test_a_file_that_sha256sums_does_not_list_stops_the_install(assets):
    sums = (assets / "SHA256SUMS").read_text().splitlines()
    (assets / "SHA256SUMS").write_text("\n".join(line for line in sums if ".lock" not in line) + "\n")
    done = _install("--from", str(assets))
    assert done.returncode == 1 and f"SHA256SUMS does not list penguwatch-{VERSION}.lock" in done.stderr


@posix_shell
def test_a_missing_file_in_the_folder_is_named(assets):
    (assets / "SHA256SUMS").unlink()
    done = _install("--from", str(assets))
    assert done.returncode == 1 and "SHA256SUMS is missing" in done.stderr


@posix_shell
@pytest.mark.skipif(sys.platform == "darwin", reason="the user layout under test is the Linux one")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root only installs the system layout")
def test_uninstall_removes_the_program_and_keeps_the_database(tmp_path):
    home = tmp_path / "home"
    data = home / ".local" / "share" / "penguwatch"
    fake = data / "app" / "venv" / "bin" / "penguwatch"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    (data / "penguwatch.db").write_text("your library")
    (data / "sessions.json").write_text("{}")
    (data / "backups").mkdir()
    done = _install("--uninstall", home=home)
    assert done.returncode == 0, done.stderr
    assert not (data / "app").exists()
    assert (data / "penguwatch.db").read_text() == "your library" and (data / "sessions.json").exists() and (data / "backups").is_dir()
    assert f"Your data is still in {data}" in done.stdout


@posix_shell
def test_uninstall_without_an_installation_changes_nothing(tmp_path):
    done = _install("--uninstall", home=tmp_path / "empty")
    assert done.returncode == 0 and "Nothing to remove" in done.stdout


# ---- release-assets.sh ----------------------------------------------------------------------------------------------------------

@posix_shell
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs GNU sha256sum")
def test_release_assets_collects_the_files_and_lists_a_checksum_for_each(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / f"penguwatch-{VERSION}-py3-none-any.whl").write_bytes(b"w")
    (dist / f"penguwatch-{VERSION}.tar.gz").write_bytes(b"s")
    out = tmp_path / "out"
    done = subprocess.run(["bash", str(RELEASE_ASSETS), str(dist), str(out)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(["SHA256SUMS", "install.ps1", "install.sh", f"penguwatch-{VERSION}-py3-none-any.whl",
                            f"penguwatch-{VERSION}.lock", f"penguwatch-{VERSION}.tar.gz"])
    listed = dict(reversed(line.split("  ")) for line in (out / "SHA256SUMS").read_text().splitlines())
    assert set(listed) == set(names) - {"SHA256SUMS"}   # the installers are in SHA256SUMS too
    for name, digest in listed.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert (out / f"penguwatch-{VERSION}.lock").read_text() == (ROOT / "requirements.txt").read_text()
    assert (out / "install.sh").read_text() == SH and os.access(out / "install.sh", os.X_OK)


@posix_shell
def test_release_assets_refuses_a_dist_folder_without_exactly_one_wheel(tmp_path):
    (tmp_path / "dist").mkdir()
    done = subprocess.run(["bash", str(RELEASE_ASSETS), str(tmp_path / "dist"), str(tmp_path / "out")],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 1 and "exactly one penguwatch-*.whl" in done.stderr


# ---- tools, when they are there; CI and the docs ---------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
@pytest.mark.parametrize("name", ["install.sh", "release-assets.sh", "ci-install-check.sh"])
def test_shell_scripts_pass_shellcheck(name):
    shell = "sh" if name == "install.sh" else "bash"
    done = subprocess.run(["shellcheck", "-s", shell, "--severity=warning", str(ROOT / "scripts" / name)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell (pwsh)")
def test_install_ps1_parses_with_powershell():
    command = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{ROOT / 'scripts' / 'install.ps1'}', [ref]$t, [ref]$e); if ($e.Count) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    done = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


def test_ci_has_an_install_job_on_the_three_systems_that_installs_the_release_files_and_checks_the_result():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("\n  install:\n", 1)[1]
    assert "needs: release-assets" in job and "os: [ubuntu-24.04, macos-latest, windows-latest]" in job
    assert "sh release-assets/install.sh --from release-assets --yes --no-browser" in job
    assert r".\release-assets\install.ps1 -From release-assets -NoBrowser" in job
    for phase in ("installed release-assets", "token", "upgraded release-assets", "removed", "diagnose"):
        assert f"scripts/ci-install-check.sh {phase}" in job
    assert job.index("install.sh --from") < job.index("ci-install-check.sh installed") < job.index("ci-install-check.sh token") \
        < job.index("ci-install-check.sh upgraded") < job.index("--uninstall --yes") < job.index("ci-install-check.sh removed")
    for use in re.findall(r"uses: (\S+)", ci):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", use), use   # every action is pinned by commit
    assert ci.count("permissions:") >= 1 and "contents: read" in ci.split("jobs:", 1)[0]


def test_ci_install_check_looks_where_the_installers_put_things():
    check = (ROOT / "scripts" / "ci-install-check.sh").read_text(encoding="utf-8")
    assert f"pg={service.LINUX_APP_DIR}/venv/bin/penguwatch" in check and f"data={service.SYSTEM_DATA_DIR}" in check
    assert f'data="{service.MAC_DAEMON_DATA_DIR}"' in check and f"pg={service.MAC_APP_DIR}/venv/bin/penguwatch" in check
    assert f"port={paths.DEFAULT_PORT}" in check and "Programs/PenguWatch/venv/Scripts/penguwatch.exe" in check
    assert f"awk -v s=\"${{score:-99}}\" 'BEGIN {{ exit !(s <= {service.MAX_EXPOSURE}) }}'" in check


def test_the_docs_state_the_default_address_and_the_commands():
    text = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    assert f"http://{paths.DEFAULT_HOST}:{paths.DEFAULT_PORT}" in text
    for section in ("## Windows", "## macOS", "## Linux", "## Raspberry Pi", "## Container", "## What has been tested where",
                    "## Upgrade", "## Uninstall", "## Backing up the data", "## Settings and secrets"):
        assert section in text, section
    for command in ("penguwatch service install", "penguwatch backup", "penguwatch doctor", "install.ps1 -Uninstall", "--from DIR"):
        assert command in text, command
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "releases/latest/download/install.ps1" in readme and "releases/latest/download/install.sh" in readme
    assert f"{service.MAX_EXPOSURE}" in text   # the exposure CI enforces is the one the docs quote
