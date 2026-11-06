# Security policy

PenguWatch holds your library and, if you use a library service such as SIMKL (the default), the login to your account, and it runs on a home network. Security
reports are welcome and taken seriously.

## Reporting a vulnerability

Please report it privately, not in a public issue:

1. Open the [Security tab](https://github.com/pengupowered/penguwatch/security) of this repository.
2. Choose **Report a vulnerability** and describe what you found: the version (the System tab, or
   `penguwatch --version`), how it is installed (container, `install.sh`, `install.ps1`, uv or pipx), the steps to
   reproduce, and what an attacker gains.

Only the maintainers see the report. Leave out your own details (addresses, names, tokens, your library): made-up
values show the problem just as well. Never paste a real access token or tracker login into a report.

You can expect a first answer within a week. PenguWatch is maintained in spare time, so a fix may take longer; you will
be told the plan, and credited in the advisory and the changelog unless you prefer not to be.

## Supported versions

Only the latest release gets security fixes. Upgrading is described in [docs/install.md](docs/install.md#upgrade).

## Scope

In scope: the PenguWatch app (its web page and API, the access token and request guards, the stored tracker login and
settings, the webhooks), the drivers, the install scripts (`install.sh`, `install.ps1`), and the published container
image and Python package.

Out of scope:

- An instance that is reachable by people you do not trust **without an access token**. That is a setup problem, not a
  vulnerability; [docs/security.md](docs/security.md) says how to set the token.
- Bugs in the tracker services themselves (SIMKL, TVmaze) or their APIs, which belong with those services.

How PenguWatch protects itself (the access token, the request guards, the container, what is exposed, the dependency
policy) is in [docs/security.md](docs/security.md) and [docs/supply-chain.md](docs/supply-chain.md).
