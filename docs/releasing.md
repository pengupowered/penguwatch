# Releasing

A release commit is tested once. It is pushed to a `ci/<name>` branch first, where `ci.yml` tests it (the unit tests,
the wheel and sdist built once, the image built from that wheel on amd64 and arm64 and started with no network to get
200 from `/api/health`, the wheel installed natively on Linux arm64, macOS and Windows, the release files
assembled and installed with `install.sh` and `install.ps1` on Linux, macOS and Windows). When that run is green, the same commit is fast-forwarded onto `main` and tagged `vX.Y.Z`; `main` does not
run CI again. The tag starts `.github/workflows/release.yml`, which does not run the tests: its first job, "Tested
commit", looks up the successful push run of `ci.yml` for exactly the tagged commit (`scripts/find-tested-run.sh`) and
fails with "no successful CI run for <sha>: push it to a ci/ branch first" if there is none. Every later job downloads
that run's artifacts (`dist`, `release-assets`) instead of rebuilding them, so the release publishes the very files
that were tested. Then:

1. builds the image from that same wheel on a native runner for `linux/amd64` and `linux/arm64` (no QEMU) and joins the
   two into one multi-architecture image at `ghcr.io/pengupowered/penguwatch` as `X.Y.Z` and `latest` (the
   per-architecture tags `X.Y.Z-amd64` and `X.Y.Z-arm64` stay in the registry; use `X.Y.Z`);
2. makes signed build provenance attestations (`actions/attest-build-provenance`) for the release files and for the
   image digest;
3. creates the GitHub release with that version's section of `CHANGELOG.md` as its notes and these files attached: the
   wheel, the sdist, `penguwatch-X.Y.Z.lock` (the hash-locked dependencies the image installs), the two install scripts
   (`install.sh`, `install.ps1`) and `SHA256SUMS`, which lists all of them;
4. last, after you approve it, publishes the wheel and sdist to PyPI.

Only the PyPI job and the two attestation jobs need `id-token: write`, and only the release job has `contents: write`.
The jobs that download from the CI run also have `actions: read` (needed to read another run's artifacts). The CI
artifacts are kept for 30 days, so a tag must follow the green `ci/` run within that time. All actions are pinned by
full commit SHA; when updating one, pick a release at least two weeks old and check that it runs on node24.

## One-time setup (before the first tag)

PyPI uses trusted publishing: no token or password exists anywhere.

1. On PyPI, add a *pending publisher* for the project `penguwatch` (Your projects, Publishing): owner `pengupowered`,
   repository `penguwatch`, workflow `release.yml`, environment `pypi`.
2. On GitHub, create the environment `pypi` (Settings, Environments) with *Required reviewers* set to the maintainer.
   The `PyPI` job waits there until it is approved, which is the last chance to stop a release.
3. Settings, Actions, General: workflow permissions can stay at the read-only default; the workflows ask for what
   they need.
4. Settings, Code security: enable **Private vulnerability reporting**, so that "Report a vulnerability" in the Security
   tab works (`SECURITY.md` sends reporters there).
5. After the first release: make the GHCR package public (see "First release only").

## Cut a release

1. On a feature branch, with the tests passing locally (`scripts/test.sh`).
2. Bump the version in one place, `__version__` in `src/penguwatch/__init__.py` (`pyproject.toml` reads it). Also change
   the image tag in `compose.release.yaml` and the example tag in `docs/install.md`
   (`tests/test_distribution.py` checks the compose file).
3. In `CHANGELOG.md`, move what is under `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section, leave
   `[Unreleased]` empty, and update the two compare links at the bottom. Describe changes for people who run
   PenguWatch, in plain words. `scripts/changelog-section.sh X.Y.Z` prints what the release notes will be.
4. Commit as a single release commit (`chore: release X.Y.Z`).
5. Push that commit to a candidate branch: `git push origin HEAD:refs/heads/ci/release-X.Y.Z`. Wait for the CI run of
   that branch to turn green (Actions tab). Fix and push a new commit if it fails.
6. Fast-forward `main` to the same commit and tag it: `git tag -s vX.Y.Z -m "PenguWatch X.Y.Z"`, then
   `git push origin HEAD:refs/heads/main vX.Y.Z`. The commit must be exactly the one that was tested: a rebase, a
   squash or an amended commit has a new hash and the Release workflow stops at "Tested commit".
7. Watch the Release workflow in the Actions tab. When the `PyPI` job asks for approval, check that the release page
   shows the notes and the four files and that `docker pull ghcr.io/pengupowered/penguwatch:X.Y.Z` works, then
   approve. Afterwards `uv tool install penguwatch` should find it.

The workflow stops before publishing anything if the tag does not match `__version__` or the changelog has no notes for
it. Before pushing you can run what CI runs: `scripts/test.sh`, then
`podman build --target wheel-built -o type=local,dest=dist .` and `scripts/test-container.sh --prebuilt`, and
`scripts/release-assets.sh dist release-assets` to see the release files.

## The first public release

The project's first public release is a **fresh repository**, not a push of the development history: one signed commit
holding the whole tree, with no history behind it. This keeps anything private that ever sat in a development commit,
a commit message or a branch name out of the public repository. The steps:

1. Finish the release on the development repository: tests green, `CHANGELOG.md` has the section for the version.
2. Run a **privacy and credential scan** over the tree you are about to publish: every tracked file, every image's
   metadata and the commit author. It must be clean (no personal names, host names or addresses, no tokens or keys, no
   metadata chunks in images, no JPEGs). Use whatever scanner you trust; the point is that it runs on exactly the
   files that go out, and fails the release on any hit.
3. Export the tree without history (`git archive` of the release commit into an empty directory), run
   `git init` there, and create the one commit with your signing key and the public identity
   (`git commit -S`, message `PenguWatch X.Y.Z`). Check that it shows as verified.
4. Create the empty repository on GitHub, add it as a remote, and follow "Cut a release" from step 5: push the commit
   to a `ci/release-X.Y.Z` branch so `ci.yml` tests it, then fast-forward `main` to that same commit and push the signed
   tag. Do the one-time setup above first.
5. Later releases are single signed commits on top of that one (pull requests are applied into them, with the
   contributor credited as a co-author, see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## First release only

The first push to GitHub Container Registry creates the package as private. In the package settings on GitHub (Package
settings, Danger Zone, Change visibility) make it public once, or `docker compose up` on other machines cannot pull it.
Link the package to the repository there as well.

## Notes

- Only tags like `v1.2.3` publish; a tag with a suffix (`v1.2.3-rc1`) does not trigger the workflow.
- `latest` moves with every release. The compose file pins an exact version so upgrades are deliberate.
- A version on PyPI cannot be changed or withdrawn, only yanked, which is why that job is last. If it fails after the
  approval, re-run the job; PyPI refuses a file it already has, so nothing is published twice.
- A pull request is tested by its own CI run, but that run is not the release's: only a push run on a `ci/` branch of
  the tagged commit counts.
- A failed release workflow can be re-run from the Actions tab; pushing the same image tags again overwrites them.
  The GitHub release step fails if the release already exists; delete that release and re-run the job.
- The workflows are checked with [actionlint](https://github.com/rhysd/actionlint) before they are pushed.
