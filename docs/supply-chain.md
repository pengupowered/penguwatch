# Supply-chain policy and update procedure (PenguWatch)

The dependency checks here use depguard, an optional external tool (a separate project; any
equivalent hash-locking and advisory-audit tooling works for the same policy). Reference for all of its commands, exit
codes and file formats: the depguard README. Commands below run from the project directory; `$DG` stands for your
checkout of `depguard.sh`. All Python tooling runs in throwaway containers; nothing is installed on the host.

## Policy

| Rule | Effect |
|---|---|
| Any known advisory (PyPI advisory DB or OSV) for a locked `name==version` | blocks, any severity |
| Advisory id starting with `MAL-` (malicious package) | always blocks, can never be ignored |
| Release younger than 7 days (cooldown) | blocks; lowered only with `--cooldown-days N --reason "..."`, recorded as `override-reason` in the lock header |
| Release yanked on PyPI | blocks (never waived) |
| Tool, network or parse error | exit 2, a failure, never treated as clean |
| Fixable Debian package in the built image (`os-scan`) | blocks (exit 1) |
| Debian package with no fix yet | reported only |

Every dependency is installed from a hash-locked file (`requirements.txt`,
`requirements-build.txt`; the test image also `requirements-dev.txt`) with
`pip install --require-hashes --no-deps --only-binary :all:`. The base image is pinned by digest.
The container runs as uid 10001 (an existing root-owned `penguwatch_data` volume needs a one-time
`chown -R 10001:0`).

These lock files are the dependency record: every package, its exact version and its hashes. GitHub reads them
for the repository's dependency graph and Dependabot alerts, and exports an SBOM from it (Insights → Dependency
graph → Export SBOM). `$DG sbom .` writes a CycloneDX SBOM locally when one is needed.

## Updating dependencies

1. Edit `requirements.in`, `requirements-dev.in` or `requirements-build.in`.
2. Lock:
   `$DG lock .`
3. Read the printed diff: `+ added`, `- removed`, `~ changed`, flags `NEW (unapproved)`, `age Nd`,
   `COOLDOWN`. A new package that you expect is accepted with `--approve-new` (it is then added to
   `.depguard-approved.txt`). The same diff is visible with `git diff` on `requirements*.txt`.
4. Run the tests:
   `scripts/test.sh` (`--seed` also runs the characterization tests, if you keep a `.seed/` library)
5. Deploy: `scripts/deploy.sh`.
   It first runs `depguard check` (locks match the `.in` files) and `depguard audit`, and refuses to
   build if either fails. `--gate-only` runs just that gate and exits. `--dry-run` runs the gate, then only prints the steps.

A security fix younger than 7 days: lock with
`$DG lock <dir> --cooldown-days 0 --reason "GHSA-xxxx fixed in 1.2.3, released yesterday"`.
The reason stays in the lock header and in git history.

Base image: `$DG base-image .` compares the
`FROM ...@sha256:` pins of both Dockerfiles with Docker Hub. To bump, replace the digest in
`Dockerfile` and `Dockerfile.test`, rebuild, run the tests, run `base-image`
again; details in the depguard README, "Base image digest check". Image OS packages:
`$DG os-scan localhost/penguwatch:latest`; if it blocks, rebuild so
the `apt-get upgrade` layer picks up the fix.

## Ignoring an advisory (with expiry)

Create `.depguard-ignore.yaml` in the project root:

```yaml
ignores:
  - id: GHSA-h5c8-rqwp-cp95        # advisory id or any alias (CVE-..., PYSEC-...)
    package: jinja2
    reason: "template sandbox is never used; fix blocked by cooldown"
    expires: 2026-11-30            # YYYY-MM-DD
```

All four keys are required. The id may be any alias of the advisory; the package must match; an
expired entry is a problem. `MAL-` ids are never ignorable. Any problem in the file (missing reason,
expired, syntax, `MAL-`) makes the audit exit 1, so a broken file cannot silently pass. Pick a short
expiry and remove the entry once the dependency is updated.

## Weekly audit

`$DG weekly` runs, for every project listed in depguard's `weekly.json` (for example this one and your other apps): `check`,
`audit`, `base-image`, and `os-scan` on the image the live service runs. It alerts on any blocking finding,
a stale lock, a tool error, an ignore expiring within 14 days, a lock older than 30 days, and
fixable OS packages (a base-image update alone is shown, not alerted).

Results, git-ignored, in depguard's `status/` directory: `latest.txt` (summary), `latest.json`,
`latest-alert.txt`, `history.log` (one line per alerting run). An alert posts to ntfy when
`DEPGUARD_NTFY_URL` is set in depguard's environment file.

A systemd user timer (depguard ships an installer for one; for example Sundays 09:00) can run it; with linger enabled it
runs without a login.

## Not covered

- Trust on first lock: hashes prove the same files are installed later, not that the first upload was
  benign. The 7-day cooldown and `MAL-` advisories reduce, not remove, that risk.
- No signature or attestation verification of wheels.
- Debian packages come from apt (signed repositories) and are not hash-pinned; the `apt-get upgrade`
  layer makes image builds not bit-reproducible.
- The Trivy vulnerability database is downloaded unpinned (rebuilt daily); only the Trivy image is pinned.
- `pip` inside the image is whatever the pinned base image ships.
- The lock files and the dependency graph leave out the OS layer; `os-scan` covers it.
- depguard's own tools are hash-locked, but the depguard repo itself is trusted.
- The weekly audit runs only while the Linux host is up (a missed run starts at the next boot: `Persistent=true`).
- An advisory published after a lock is caught only by the weekly audit or the next deploy.
- Front end: no third-party JavaScript or CSS libraries; scripts and styles are served from `/static`.
  Poster and logo images are loaded at runtime from the catalogue services' image hosts (SIMKL's with the default driver, and TVmaze's when that
  driver is used; CSP `img-src 'self' data: https:`), and links to the services' sites and imdb.com are plain navigation.
