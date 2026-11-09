"""Installing and releasing: the release compose file, the changelog and the GitHub workflows. These files sit at the
repository root, so Dockerfile.test copies them in."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from penguwatch import __version__

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML reads the key `on` as True
    return data


def _service(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]["penguwatch"]


def test_release_compose_pins_the_current_version():
    service = _service("compose.release.yaml")
    assert service["image"] == f"ghcr.io/pengupowered/penguwatch:{__version__}"
    assert "build" not in service
    assert "./config.yaml:/app/config.yaml:ro" in service["volumes"]


@pytest.mark.parametrize("name", ["docker-compose.yml", "compose.release.yaml"])
def test_both_compose_files_run_with_a_read_only_root_and_no_privileges(name):
    """The image writes only to the data volume and /tmp (scripts/test-container.sh checks that on the running image)."""
    service = _service(name)
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp"]
    assert service["cap_drop"] == ["ALL"]
    assert any(v.startswith("penguwatch_data:/app/data") for v in service["volumes"])


def test_release_compose_passes_the_same_environment_as_the_build_compose():
    assert _service("compose.release.yaml")["environment"] == _service("docker-compose.yml")["environment"]


@pytest.mark.parametrize("name", ["docker-compose.yml", "compose.release.yaml"])
def test_the_settings_variables_are_passed_only_when_set(name):
    """A variable that is set pins its setting on the Settings tab, so log level and digest kind and hour must reach
    the container empty (which counts as unset), not as the old defaults."""
    env = dict(line.split("=", 1) for line in _service(name)["environment"])
    for var in ("PENGUWATCH_LOG_LEVEL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR"):
        assert env[var] == "${%s:-}" % var


def test_the_apt_upgrade_layer_is_refreshed_by_a_date_argument():
    for name in ("Dockerfile", "Dockerfile.test"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"(?m)^ARG APT_REFRESH=$\n^RUN apt-get update && apt-get -y .*upgrade", text), name
    assert _service("docker-compose.yml")["build"]["args"]["APT_REFRESH"] == "${APT_REFRESH:-}"
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert re.search(r'APT_REFRESH="\$\(date -u \+%Y-%m-%d\)"; export APT_REFRESH\n\s*run podman-compose build', deploy)


def test_deploy_backup_includes_secrets_json_privately():
    text = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "data/secrets.json" in text and "secrets.json" in text.split("rollback point", 1)[1]
    assert "umask 077" in text


def test_the_repository_has_no_file_named_compose_yaml():
    """Docker and Podman prefer compose.yaml over docker-compose.yml, which would hijack the source build and deploy.sh."""
    assert not (ROOT / "compose.yaml").exists()


def test_changelog_has_unreleased_and_a_section_for_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    notes = subprocess.run([str(ROOT / "scripts" / "changelog-section.sh"), __version__],
                           capture_output=True, text=True, check=False)
    assert notes.returncode == 0, notes.stderr
    assert notes.stdout.strip()
    assert "]: https://" not in notes.stdout  # the link references at the bottom are not release notes


def test_release_notes_have_no_relative_links():
    """The notes are shown on the release page, where a relative link (docs/x.md) leads nowhere."""
    import re
    for version in (__version__, "0.15.0"):
        notes = subprocess.run([str(ROOT / "scripts" / "changelog-section.sh"), version],
                               capture_output=True, text=True, check=True).stdout
        targets = re.findall(r"\]\(([^)]+)\)", notes)
        assert all(t.startswith(("https://", "http://", "#")) for t in targets), [t for t in targets if not t.startswith("http")]
        assert not [t for t in targets if t.count("://") > 1], version  # an absolute link is left as it is
    notes = subprocess.run([str(ROOT / "scripts" / "changelog-section.sh"), "0.15.0"],
                           capture_output=True, text=True, check=True).stdout
    assert "](https://github.com/pengupowered/penguwatch/blob/v0.15.0/docs/drivers.md)" in notes  # rewritten
    assert "](https://www.tvmaze.com/api)" in notes  # an absolute link is kept


def test_changelog_section_fails_for_an_unknown_version():
    res = subprocess.run([str(ROOT / "scripts" / "changelog-section.sh"), "0.0.0"],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 1


def test_changelog_names_no_private_history():
    """Only the public release (0.14.0 onward) is listed."""
    versions = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert versions and all(tuple(map(int, v.split("."))) >= (0, 14, 0) for v in versions)


def test_pyproject_reads_the_version_from_the_package():
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"] and "version" not in project["project"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "penguwatch.__version__"}
    assert project["project"]["scripts"] == {"penguwatch": "penguwatch.cli:main"}


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflows_pin_actions_by_commit_and_declare_permissions(name):
    workflow = _load(WORKFLOWS / name)
    assert workflow["permissions"] == {"contents": "read"}  # the default for every job; jobs widen it themselves
    for job_id, job in workflow["jobs"].items():
        assert "permissions" in job or workflow["permissions"], job_id
        for step in job.get("steps", []):
            if "uses" in step:
                assert FULL_SHA.match(step["uses"]), f"{name}/{job_id}: {step['uses']} is not pinned by commit"


def test_only_the_publishing_jobs_can_write():
    release = _load(WORKFLOWS / "release.yml")
    writers = {job_id for job_id, job in release["jobs"].items()
               if any(v == "write" for v in job.get("permissions", {}).values())}
    assert writers == {"image", "manifest", "attest-image", "attest-files", "release", "pypi"}
    assert release["jobs"]["pypi"]["environment"]["name"] == "pypi"
    assert release["jobs"]["pypi"]["permissions"] == {"id-token": "write", "actions": "read"}
    assert release["jobs"]["release"]["permissions"]["contents"] == "write"
    ci = _load(WORKFLOWS / "ci.yml")
    assert not [j for j, job in ci["jobs"].items() if "write" in job.get("permissions", {}).values()]


def test_release_workflow_runs_on_version_tags_only_and_uses_the_tested_run():
    release = _load(WORKFLOWS / "release.yml")
    assert release["on"] == {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "scripts/find-tested-run.sh" in text and "ghcr.io/pengupowered/penguwatch" in text


def test_ci_builds_the_image_from_the_wheel_it_tested():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/test-container.sh --prebuilt" in text and "scripts/test.sh" in text


def test_dockerfile_installs_the_wheel_not_the_tree():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "penguwatch-*.whl" in text and "pip install --no-cache-dir --no-deps ." not in text
    assert "USER 10001" in text and "HEALTHCHECK" not in text
    assert text.rstrip().splitlines()[-1] == 'CMD ["penguwatch", "serve"]'


def test_the_configuration_doc_lists_every_setting_and_pinning_variable():
    """docs/configuration.md is the settings table users read: a key or variable added to the registry must be in it."""
    from penguwatch.settings import registry
    from penguwatch.settings.env import OVERRIDE_VARIABLES, SECRET_ENV, SETTING_ENV

    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    missing = [s.key for s in registry.SETTINGS if f"`{s.key}`" not in text]
    assert not missing, f"not in docs/configuration.md: {missing}"
    absent = [v for v in [*SETTING_ENV.values(), *SECRET_ENV.values()] if f"`{v}`" not in text]
    assert not absent, f"variables not in docs/configuration.md: {absent}"
    from penguwatch import envfile
    # penguwatch.env's template mentions the digest variables and Grafana (the others are not in it)
    assert all(v in envfile.TEMPLATE for v in ("PENGUWATCH_NOTIFY_URL", "PENGUWATCH_NOTIFY_KIND", "PENGUWATCH_NOTIFY_HOUR"))
    assert OVERRIDE_VARIABLES  # the table above is built from the same module


SIBLING = "pengu" + "mon"   # built from two parts so that this file does not name the project either


def test_no_tracked_text_file_names_the_sibling_project():
    """No text file of the repository names the sibling project (any case). The copied tree of the test image has the
    same files as the checkout: the folders Dockerfile.test copies and the top-level files. Binary files and the
    requirements files are skipped."""
    folders = [ROOT / name for name in (".github", "docs", "scripts", "src", "tests")]
    paths = [p for folder in folders for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    paths += [p for p in ROOT.iterdir() if p.is_file() and p.name != ".git"]
    hits = []
    for path in sorted(paths):
        if path.name.startswith("requirements") and path.suffix == ".txt":
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if SIBLING in text.lower():
            hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"these files name the sibling project: {hits}"
