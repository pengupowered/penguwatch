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
    assert "pengumon" not in text.lower()


def test_ci_builds_the_image_from_the_wheel_it_tested():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/test-container.sh --prebuilt" in text and "scripts/test.sh" in text
    assert "pengumon" not in text.lower()


def test_dockerfile_installs_the_wheel_not_the_tree():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "penguwatch-*.whl" in text and "pip install --no-cache-dir --no-deps ." not in text
    assert "USER 10001" in text and "HEALTHCHECK" not in text
    assert text.rstrip().splitlines()[-1] == 'CMD ["penguwatch", "serve"]'
