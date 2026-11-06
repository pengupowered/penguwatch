# PenguWatch documentation

Every document, grouped by what you are trying to do. The [project README](../README.md) is the short version.

## Getting started

- [Getting started](getting-started.md): the first run end to end: install, open the page, set up your services and
  household, connect the library service (SIMKL by default) or choose local plus TVmaze, and the first sync.
- [Install](install.md): per-system instructions (Windows, macOS, Linux, Raspberry Pi, container, uv or pipx), the
  background service, the settings file, upgrading, backing up and uninstalling.
- [FAQ and troubleshooting](faq.md): the common problems and how to fix them.

## Using it

- [Using PenguWatch](usage.md): the tabs, tracking episodes, the optimizer and rotation planner, the calendar feed,
  shortcuts and the phone app.
- [Screenshots](screenshots.md): the pages and dialogs, in both themes.
- [Configuration](configuration.md): every `config.yaml` key and environment variable, the household, services and
  bundles, and how a title gets its service.

## Running it

- [Security](security.md): the access token, the request guards, the container and what is exposed.
- [Operations](operations.md): monitoring, Prometheus, OpenTelemetry, notifications, backups and exports.
- [Sync](sync.md): how the sync engine works (outbox, review mode, removals, enrichment) and the SIMKL driver's specifics.

## Extending it

- [Drivers](drivers.md): the drivers that ship (SIMKL, local, TVmaze) and a guide to writing one.
- [API](api.md): authentication, the stable endpoints and their shapes, errors and limits.
- [Extending](extending.md): your own rating sources, event webhooks, drivers and components.
- [Architecture](architecture.md): the ordered backend components, start-up failures, shutdown and the driver layer.

## Project

- [Development](development.md): project layout, running locally, tests, migrations.
- [The web UI](ui.md): how the page is built, theming, adding a tab and the rules the tests enforce.
- [Releasing](releasing.md): CI, the release workflow and the first public release.
- [Supply chain](supply-chain.md): the dependency policy and how dependencies are updated.
- [Contributing](../CONTRIBUTING.md), [Security policy](../SECURITY.md) and the [changelog](../CHANGELOG.md).
