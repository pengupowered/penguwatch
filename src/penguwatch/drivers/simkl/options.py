"""The SIMKL driver's options: config.yaml's `simkl:` section."""

from pydantic import BaseModel


class SimklOptions(BaseModel):
    client_id: str = ""
    access_token: str = ""
    # How often to check SIMKL for changes (cheap: one /sync/activities call unless something changed)
    auto_sync_interval_minutes: int = 30
    # After this many hours without a user interaction, pull only every idle_sync_interval_minutes
    idle_after_hours: int = 12
    idle_sync_interval_minutes: int = 180
    app_name: str = "PenguWatch"
