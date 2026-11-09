"""The appliers of the settings that have no component of their own: the clock's zone, the log level, the Grafana
links and the household's members. The settings component calls `register_core` right after it creates the hub.

It registers
  "clock.zone"         keys server.timezone       clock.configure(config.server.timezone)
  "logging.level"      keys server.log_level      observability.set_level(config.server.log_level)
  "grafana.links"      keys display.grafana_url   grafana.configure(config.display.grafana_url)
  "household.members"  keys household.members     repository.set_household_members(the member ids)  (with a repository)
reading the live `config`, which the manager has already updated in place when the hub publishes."""

from penguwatch import clock, grafana, observability
from penguwatch.config import AppConfig
from penguwatch.settings.hub import Change, SettingsHub


def register_core(hub: SettingsHub, config: AppConfig, repository=None) -> None:
    """Subscribes the core appliers to `hub`. Each reads `config`, which the manager has already updated in place.
    `repository`: the database repository, which keeps the household's member ids (which of a title's stored people
    count); without one that applier is left out."""

    @hub.on(keys=["server.timezone"], name="clock.zone")
    def _zone(change: Change) -> None:
        clock.configure(config.server.timezone)  # "" = the TZ variable, else UTC (clock._resolve)

    @hub.on(keys=["server.log_level"], name="logging.level")
    def _level(change: Change) -> None:
        observability.set_level(config.server.log_level)

    @hub.on(keys=["display.grafana_url"], name="grafana.links")
    def _grafana(change: Change) -> None:
        grafana.configure(config.display.grafana_url)

    if repository is not None:
        @hub.on(keys=["household.members"], name="household.members")
        def _members(change: Change) -> None:
            repository.set_household_members(config.household.ids)
