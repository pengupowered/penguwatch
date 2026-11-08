"""A settings store on a test container: the manager the settings component makes, on the container's database and a
secrets.json in the test's folder."""

from pathlib import Path
from typing import Mapping, Optional

from penguwatch.config import EnvOverrides
from penguwatch.secrets import SecretStore
from penguwatch.settings.hub import SettingsHub
from penguwatch.settings.manager import SettingsManager


async def attach_manager(container, folder: Path, raw: Optional[dict] = None, *, config_name: str = "config.yaml",
                         environ: Optional[Mapping[str, str]] = None, hub: Optional[SettingsHub] = None,
                         token_set=lambda: False) -> SettingsManager:
    """Runs the one-time import of `raw` (a config.yaml mapping; {} for a file that sets nothing) on the container's
    database and attaches the manager as container.settings, as the settings and api components do."""
    manager = SettingsManager(container.config, container.repository, SecretStore(Path(folder) / "secrets.json"),
                              hub or SettingsHub(), EnvOverrides(), environ, config_path=str(Path(folder) / config_name),
                              file_raw=raw if raw is not None else {}, token_set=token_set)
    await manager.load()
    container.settings = manager
    return manager
