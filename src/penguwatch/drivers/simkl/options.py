"""The SIMKL driver's options: config.yaml's `simkl:` section. The Settings tab shows the three intervals (Sync) and
`app_name` (Advanced) with these ranges and defaults (penguwatch.settings.registry); the login is not a setting."""

from pydantic import BaseModel, Field, field_validator, model_validator


class SimklOptions(BaseModel):
    client_id: str = ""
    access_token: str = ""
    # How often to check SIMKL for changes (cheap: one /sync/activities call unless something changed)
    auto_sync_interval_minutes: int = Field(default=30, ge=5, le=1440)
    # After this many hours without a user interaction, pull only every idle_sync_interval_minutes
    idle_after_hours: int = Field(default=12, ge=1, le=168)
    idle_sync_interval_minutes: int = Field(default=180, ge=15, le=1440)
    # The name in the User-Agent of every request to SIMKL
    app_name: str = Field(default="PenguWatch", min_length=1, max_length=40)

    @field_validator("app_name")
    @classmethod
    def _printable(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable() or not value.isascii():
            raise ValueError("simkl.app_name: 1 to 40 printable ASCII characters (it is sent in a request header)")
        return value

    @model_validator(mode="after")
    def _idle_not_more_often(self):
        if self.idle_sync_interval_minutes < self.auto_sync_interval_minutes:
            raise ValueError("simkl.idle_sync_interval_minutes must not be below auto_sync_interval_minutes")
        return self
