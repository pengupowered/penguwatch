"""The TVmaze driver's options: config.yaml's `tvmaze:` section. TVmaze needs no account or key."""

from pydantic import BaseModel, Field


class TvmazeOptions(BaseModel):
    # Requests per rate_window_seconds. TVmaze allows at least 20 per 10 s per address; stay a little under
    rate_limit_calls: int = Field(default=18, ge=1, le=20)
    rate_window_seconds: float = Field(default=10.0, gt=0, le=60)
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    # How many library titles the schedule checks per run (one request each)
    schedule_max_titles: int = Field(default=60, ge=1, le=500)
