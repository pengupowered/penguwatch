# Contributing to PenguWatch

Thanks for helping. Bug reports, ideas, documentation and code are all welcome. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md). A security problem goes through the private process in [SECURITY.md](SECURITY.md),
not an issue.

## Reporting a bug or asking for a feature

Open an [issue](https://github.com/pengupowered/penguwatch/issues/new/choose) and pick the form that fits. For a bug, the
most useful details are the version (the System tab, or `penguwatch --version`), how PenguWatch is installed, the drivers
you use, what you expected and what happened, and the log lines around it. The [FAQ](docs/faq.md) covers the common
problems.

**Keep your own data private.** A bug report is public. Leave out access tokens, library-service client IDs and tokens (SIMKL's, say), webhook
secrets and anything that names you or your household; replace titles from your library with made-up ones if they
matter.

## Contributing code

For anything beyond a small fix, open an issue first, so we can agree on the approach before you spend time on it.

### How a pull request becomes part of PenguWatch

The public history is one signed commit per release ([docs/releasing.md](docs/releasing.md)), so a pull request is not
merged as it is. It is reviewed and tested, then applied into the next release commit, with you credited as a co-author
(`Co-authored-by:`) and in the changelog. The pull request is then closed with a link to that release.

By contributing you agree that your contribution is licensed under the project's [MIT license](LICENSE).

### Setting up

You need Python 3.11 or newer, or only Podman (or Docker) to run the tests in a container:

```bash
pip install --require-hashes --no-deps -r requirements-dev.txt
PYTHONPATH=src pytest -q                      # the tests, with the tracker services mocked
PYTHONPATH=src python -m penguwatch.main      # the app on http://localhost:8080, using ./config.yaml or the example
scripts/test.sh                               # the same tests in a throwaway container with no network
```

`scripts/test.sh` builds `Dockerfile.test` into a test image and runs the whole suite in it; extra arguments go to
pytest. [docs/development.md](docs/development.md) has the project layout, database migrations and the characterization
tests; [docs/architecture.md](docs/architecture.md) explains how the backend is put together. For a try-out library of
made-up titles, run `scripts/demo_library.py`.

### The rules the tests enforce

These fail the build, so it is quicker to know them first ([docs/ui.md](docs/ui.md) explains each one):

- **No inline styles or scripts:** no `style` attribute or block, no inline event handler (the CSP forbids them).
- **No colour literals outside the tokens:** hex, `rgb()` and `hsl()` colours live only in `tokens.css`; both themes
  define the same tokens.
- **AA contrast** (4.5:1) for text colours in both the dark and the light theme.
- **No service names under `web/` or outside a driver:** the page and the core know no tracker service. Everything
  service-specific lives in the service's driver package and reaches the page through `/api/drivers`.
- **No emoji in the UI,** and every tab and dialog is a complete folder that imports only the shell and its own files.
- **Images carry no metadata:** PNGs hold only their structural chunks (no EXIF, text or time), and there are no JPEGs.

### What a good pull request looks like

- **Tests:** a fix comes with a test that fails without it. Keep tests lean: one meaningful test beats several that
  check the same thing. `scripts/test.sh` passes; CI runs it on your pull request.
- **No new dependencies** without agreeing on it in an issue first. Dependencies are hash-locked
  (`requirements*.txt`, generated from the `.in` files) and checked against advisories before every release; a new
  package needs approval ([docs/supply-chain.md](docs/supply-chain.md)).
- **A new service is a driver.** Check the service's terms first, then follow [docs/drivers.md](docs/drivers.md);
  open a "Support for a new driver" [issue](https://github.com/pengupowered/penguwatch/issues/new/choose) before you
  start.
- **Plain words** on screen and in the docs: what a home user understands. Dark and light themes and a phone width of
  390 px must both work.
- **Made-up data in tests, examples and images:** invented titles (`scripts/demo_library.py` has some), addresses from
  192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24, names like "Alex" and "Sam", and the domain example.com. Never data
  from a real library, account or network.
- **Commits:** small and focused, with a message that says what changed and why in the first line, in the imperative
  ("Fix the ...", "Add ..."). Squash fix-up commits before you ask for review.
- **Docs:** update the docs your change affects. Leave the version number and `CHANGELOG.md` to the maintainers.
