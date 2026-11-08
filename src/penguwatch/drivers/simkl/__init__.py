"""The SIMKL driver (https://simkl.com): a tracker with an account. It offers every capability: the catalogue
(search, details, episode lists, sequels), the account's library (device-PIN login with token renewal, a delta pull
along SIMKL's /sync/activities, pushes), the airing schedule, trending lists and recommendations, and the SIMKL and
IMDb (MyAnimeList for anime) ratings.

This module stays light (the registry imports it): `build` imports the parts that talk to SIMKL."""

from typing import Any, ClassVar, Dict, FrozenSet, Mapping, Tuple

from penguwatch.drivers.base import BuiltinRatingSource, Capability, DriverSetting
from penguwatch.drivers.simkl.options import SimklOptions


class SimklDriver:
    name: ClassVar[str] = "simkl"
    label: ClassVar[str] = "SIMKL"
    offers: ClassVar[FrozenSet[Capability]] = frozenset(Capability)
    options_model: ClassVar[type] = SimklOptions
    # (env var, option) the environment overrides
    env_options: ClassVar[Tuple[Tuple[str, str], ...]] = (("SIMKL_CLIENT_ID", "client_id"),
                                                           ("SIMKL_ACCESS_TOKEN", "access_token"))
    rating_sources: ClassVar[Tuple[BuiltinRatingSource, ...]] = (
        BuiltinRatingSource(id="simkl", name="SIMKL", label="SIMKL", column="simkl_rating"),
        BuiltinRatingSource(id="imdb", name="IMDb (MyAnimeList for anime)", label="IMDb", column="imdb_rating",
                            votes_column="imdb_votes"),
    )
    secret_state_keys: ClassVar[Tuple[str, ...]] = ("access_token", "refresh_token", "client_id")
    key_provider: ClassVar[str] = "simkl"
    remote: ClassVar[bool] = True
    connect_kind: ClassVar[str] = "device_code"
    extras: ClassVar[Tuple[str, ...]] = ("credentials", "diagnose")  # an app id form, a connection check
    # a title's page on SIMKL, by media type
    title_urls: ClassVar[Dict[str, str]] = {"show": "https://simkl.com/tv/{id}", "anime": "https://simkl.com/anime/{id}",
                                            "movie": "https://simkl.com/movies/{id}"}
    home_url: ClassVar[str] = "https://simkl.com"  # credited on the Services & Sync tab
    health_url: ClassVar[str] = "https://api.simkl.com/"  # what `penguwatch doctor` checks is reachable
    # The Settings tab (group "SIMKL"): the intervals under Sync, the User-Agent name under Advanced
    settings: ClassVar[Tuple[DriverSetting, ...]] = (
        DriverSetting("auto_sync_interval_minutes", "sync", "Check for changes every",
                      "How often PenguWatch asks your SIMKL account for changes while the app is in use.", unit="min"),
        DriverSetting("idle_after_hours", "sync", "Quiet after",
                      "With nobody using the app for this long, PenguWatch checks less often.", unit="h"),
        DriverSetting("idle_sync_interval_minutes", "sync", "Check while quiet every",
                      "How often the account is checked after a quiet spell. Not below the check above.", unit="min"),
        DriverSetting("app_name", "advanced", "App name sent to SIMKL",
                      "The name in the User-Agent of every request to SIMKL.", live=False),
    )
    not_on_page: ClassVar[Mapping[str, str]] = {
        "client_id": "a credential: the stored login (Services & Sync) wins; the import moves a file value there",
        "access_token": "a credential: the stored login (Services & Sync) wins; the import moves a file value there",
    }
    # 0.15 / 80aebed defaults (frozen; its packaged example wrote the same values)
    legacy_defaults: ClassVar[Mapping[str, Any]] = {
        "auto_sync_interval_minutes": 30, "idle_after_hours": 12, "idle_sync_interval_minutes": 180,
        "app_name": "PenguWatch",
    }

    @classmethod
    async def build(cls, repository, options, *, app_version: str, on_request=None):
        """The running driver: the client with the stored login, and its capability objects."""
        from penguwatch.drivers.simkl.service import SimklService
        return await SimklService.create(repository, options, app_version=app_version, on_request=on_request)
