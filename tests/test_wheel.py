"""The wheel: built the way the release builds it (pip wheel --no-deps --no-build-isolation), it must carry every
file the app reads at run time and nothing that is not the app; installed on its own, the `penguwatch` command runs.
Also pyproject's declared dependencies against requirements.in, and the example config."""

import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from penguwatch import __version__
from penguwatch.config import AppConfig, packaged_example_text

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "penguwatch"


def _names(requirement: str) -> str:
    return re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9_.-]+", requirement).group(0)).lower()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """The wheel of a copy of the tree (the build must not leave build/ or *.egg-info in the checkout)."""
    work = tmp_path_factory.mktemp("build")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (work / name).write_bytes((ROOT / name).read_bytes())
    shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = work / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-cache-dir",
         "-q", "-w", str(out), str(work)],
        capture_output=True, text=True, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(out.glob("penguwatch-*.whl"))
    assert len(wheels) == 1
    assert wheels[0].name.startswith(f"penguwatch-{__version__}-"), wheels[0].name
    return wheels[0]


@pytest.fixture(scope="module")
def wheel(built) -> zipfile.ZipFile:
    return zipfile.ZipFile(built)


def _files_in(directory: Path):
    return sorted(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def _missing(wheel, files):
    names = set(wheel.namelist())
    return [p.relative_to(PACKAGE).as_posix() for p in files
            if "penguwatch/" + p.relative_to(PACKAGE).as_posix() not in names]


def test_wheel_carries_the_whole_static_tree_and_the_templates(wheel):
    static, templates = _files_in(PACKAGE / "web" / "static"), _files_in(PACKAGE / "web" / "templates")
    assert len(static) > 20 and templates  # the dashboard really is there to be checked
    assert _missing(wheel, static + templates) == []
    # subfolders, not only the top level (the panels and the icons live in them)
    assert any(p.parent != PACKAGE / "web" / "static" for p in static)


def test_a_new_static_subfolder_is_picked_up(tmp_path):
    """Recursive package data: a file in a folder that does not exist yet is shipped without a pyproject change."""
    work = tmp_path / "tree"
    work.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (work / name).write_bytes((ROOT / name).read_bytes())
    shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    deep = work / "src" / "penguwatch" / "web" / "static" / "panels" / "demo" / "deeper"
    deep.mkdir(parents=True)
    (deep / "marker.json").write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-cache-dir", "-q",
         "-w", str(work / "dist"), str(work)], capture_output=True, text=True, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(next((work / "dist").glob("penguwatch-*.whl"))) as z:
        assert "penguwatch/web/static/panels/demo/deeper/marker.json" in z.namelist()


def test_wheel_carries_every_python_module_the_example_config_and_the_license(wheel):
    modules = [p for p in _files_in(PACKAGE) if p.suffix == ".py"]
    assert _missing(wheel, modules) == []
    names = set(wheel.namelist())
    assert {"penguwatch/cli.py", "penguwatch/paths.py", "penguwatch/config.example.yaml",
            "homeguard/security.py"} <= names
    assert f"penguwatch-{__version__}.dist-info/licenses/LICENSE" in names


def test_wheel_has_no_tests_scripts_or_private_files(wheel):
    tops = {n.split("/")[0] for n in wheel.namelist()}
    assert tops == {"penguwatch", "homeguard", f"penguwatch-{__version__}.dist-info"}
    names = wheel.namelist()
    assert not [n for n in names if re.search(r"(^|/)(tests?|scripts)/", n)]
    assert not [n for n in names if Path(n).name in {"config.yaml", ".env", "secrets.json"}]
    assert not [n for n in names if n.endswith((".pyc", ".db"))]


def test_wheel_declares_the_penguwatch_command_and_the_dependencies(wheel):
    dist = f"penguwatch-{__version__}.dist-info/"
    assert "penguwatch = penguwatch.cli:main" in wheel.read(dist + "entry_points.txt").decode()
    meta = wheel.read(dist + "METADATA").decode().replace("\r\n", "\n")
    assert f"\nVersion: {__version__}\n" in meta
    requires = {_names(m) for m in re.findall(r"(?m)^Requires-Dist: (.+)$", meta) if "extra ==" not in m}
    assert {"fastapi", "uvicorn", "aiosqlite", "pyyaml", "opentelemetry-sdk",
            "opentelemetry-exporter-otlp-proto-http"} <= requires


def test_packaged_example_config_is_the_repository_example():
    """config.example.yaml at the root is the file people copy; the package carries a byte-identical copy for
    `penguwatch init-config`. Edit the root file, then copy it over the one in src/penguwatch/."""
    assert (PACKAGE / "config.example.yaml").read_bytes() == (ROOT / "config.example.yaml").read_bytes()
    assert packaged_example_text() == (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    config = AppConfig(**yaml.safe_load(packaged_example_text()))
    assert config.services  # the shipped example lists streaming services


def test_the_wheel_installs_and_the_command_runs(tmp_path, built):
    """Installed on its own (--no-deps; the test image has the dependencies), away from the source tree."""
    target = tmp_path / "site"
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "-q",
                             "--target", str(target), str(built)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PENGUWATCH_", "PYTHONPATH", "XDG_"))}
    env.update(PYTHONPATH=str(target), HOME=str(home), USERPROFILE=str(home))
    script = target / "bin" / "penguwatch"
    assert script.exists()
    out = subprocess.run([sys.executable, str(script), "--version"], capture_output=True, text=True, env=env,
                         cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"penguwatch {__version__}"
    # the package it ran is the installed one, with the dashboard and the example config inside
    where = subprocess.run([sys.executable, "-c", "import penguwatch, pathlib; print(pathlib.Path(penguwatch.__file__).parent)"],
                           capture_output=True, text=True, env=env, cwd=tmp_path).stdout.strip()
    assert where.startswith(str(target))
    assert (Path(where) / "web" / "static").is_dir() and (Path(where) / "config.example.yaml").is_file()
    # no config of the user's and none beside it: the app still has its streaming services (the packaged example)
    code = "from penguwatch.config import load_config; print(len(load_config().services))"
    services = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert services.returncode == 0, services.stderr
    assert int(services.stdout.strip()) > 0
    init = subprocess.run([sys.executable, str(script), "init-config"], capture_output=True, text=True, env=env,
                          cwd=tmp_path)
    assert init.returncode == 0, init.stderr
    written = Path(init.stdout.strip())
    assert str(written).startswith(str(home)) and written.read_text(encoding="utf-8") == packaged_example_text()


def test_pyproject_dependencies_cover_requirements_in():
    """requirements.in is what the image and the lock install; pyproject is what a pip/uv install of the wheel gets.
    A package only in the first one means the wheel installs and then fails to start."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    wanted = [line.strip() for line in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith(("#", "-"))]
    assert wanted
    assert {d.replace(" ", "") for d in wanted} <= {d.replace(" ", "") for d in declared}
