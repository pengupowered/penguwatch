"""The settings component: secrets.json, the settings store (settings/manager.py) and the hub components subscribe to
for live changes (settings/hub.py). Right after the storage (it needs the database) and before every feature: its
`register` makes the live config (`ctx.config`) the store's, so the drivers, the optimizer and the rest are built from
it.

1. SecretStore(<folder of the database>/secrets.json); the hub (a raising applier counts in
   penguwatch.settings.live_failures); the core appliers (settings/appliers.py).
2. The one-time import of config.yaml (the first start of 0.16), then the stored document with the environment on
   top, copied into ctx.config in place; one log line when config.yaml's settings differ from the store.
3. The catalogue refreshes the streaming services the user never edited (penguwatch.catalog).
4. What depends on the effective drivers: the repository's identity providers and the built-in rating sources.
5. The clock's zone, the log level and the Grafana links from the effective values.
6. The household's members into the repository (which of a title's stored people count); after a save that changes
   them, the core applier "household.members" does it again.
"""

from penguwatch import clock, grafana, paths, telemetry
from penguwatch.components import Component, Context
from penguwatch.config import read_config_file
from penguwatch.drivers import identity_providers
from penguwatch.observability import set_level
from penguwatch.ratings import stored_builtin_specs
from penguwatch.secrets import SecretStore
from penguwatch.settings import appliers
from penguwatch.settings.hub import SettingsHub
from penguwatch.settings.importer import refresh_catalogue
from penguwatch.settings.manager import SettingsManager


async def register(ctx: Context) -> Component:
    repo = ctx.repository
    secrets = SecretStore(paths.secrets_path(repo.db_path))
    hub = SettingsHub(on_failure=telemetry.settings_live_failure)
    appliers.register_core(hub, ctx.config, repo)
    config_path, raw = read_config_file(env=ctx.env)
    security = ctx.security
    manager = SettingsManager(ctx.config, repo, secrets, hub, ctx.env, config_path=config_path, file_raw=raw,
                              token_set=lambda: bool(getattr(security, "access_token", "") or ""))
    await manager.load()
    await refresh_catalogue(repo)
    roles = ctx.config.drivers
    repo.key_providers = tuple(identity_providers(roles.library, roles.catalog))
    await repo.seed_builtin_rating_sources(
        stored_builtin_specs([roles.library, *roles.catalog, *roles.schedule, *roles.discovery]))
    clock.configure(ctx.config.server.timezone)
    set_level(ctx.config.server.log_level)
    try:
        grafana.configure(ctx.config.display.grafana_url)
    except ValueError:  # checked by the setting's field; kept for a config built around it
        grafana.configure("")
    repo.set_household_members(ctx.config.household.ids)
    ctx.settings, ctx.settings_manager = hub, manager
    return Component("settings")
