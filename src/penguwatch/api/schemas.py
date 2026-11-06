"""Request bodies for endpoints that used to accept a bare dict.

With `Dict[str, Any]` a missing key or a wrong type surfaced as a 500 (KeyError/ValueError) and
unbounded lists/strings were accepted. These models give clients a 422 that names the field and
bound every collection.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from penguwatch.models import HouseholdTag, NormalIds, ShowStatus

IdList = List[int]


class TidyMove(BaseModel):
    show_id: int
    status: ShowStatus


class TidySequel(BaseModel):
    ids: NormalIds = Field(min_length=1)  # the sequel's ids, as the tidy suggestion gave them
    title: str = Field(default="Sequel", max_length=300)
    parent_id: Optional[int] = None


class TidyAssign(BaseModel):
    show_id: int
    service_id: str = Field(min_length=1, max_length=64)


class TidyApplyRequest(BaseModel):
    """Either show_ids + status (one status for all), or moves (a status per show), plus optional sequels
    and service assignments."""

    show_ids: IdList = Field(default_factory=list, max_length=500)
    status: Optional[ShowStatus] = None
    moves: List[TidyMove] = Field(default_factory=list, max_length=500)
    sequels: List[TidySequel] = Field(default_factory=list, max_length=100)
    assignments: List[TidyAssign] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def status_needed(self):
        if (self.show_ids or self.sequels) and self.status is None:
            raise ValueError("status is required with show_ids or sequels")
        return self


class TidyDismissRequest(BaseModel):
    kind: Literal["returning", "stale"]
    keys: List[str] = Field(default_factory=list, max_length=500)
    show_ids: IdList = Field(default_factory=list, max_length=500)
    months: int = Field(default=6, ge=1, le=60)


class ServiceToggleRequest(BaseModel):
    is_active: bool = False
    renewal_day: Optional[int] = Field(default=None, ge=1, le=31)
    renewal_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class DiscoverAddRequest(BaseModel):
    title: str = Field(default="New title", max_length=300)
    year: Optional[int] = Field(default=None, ge=1880, le=2200)
    ids: NormalIds = Field(default_factory=dict)  # as the discovered item gave them
    media_type: Literal["show", "anime", "movie"] = "show"
    status: ShowStatus = ShowStatus.PLANTOWATCH
    household_tag: HouseholdTag = HouseholdTag.BOTH
    poster: Optional[str] = Field(default=None, max_length=1000)
    overview: Optional[str] = Field(default=None, max_length=4000)


class DriverCredentialsRequest(BaseModel):
    """Values a driver's login takes by hand (an app id, a pasted token): what each means is the driver's."""

    client_id: str = Field(default="", max_length=128)
    access_token: str = Field(default="", max_length=512)


class ConnectRequest(BaseModel):
    """Continues a connect flow with the `state` the previous step returned; without it, starts one."""

    state: Optional[Dict[str, Optional[str]]] = Field(default=None, max_length=8)

    @field_validator("state")
    @classmethod
    def _short_values(cls, value):
        if value and any(v is not None and len(str(v)) > 512 for v in value.values()):
            raise ValueError("state values are at most 512 characters")
        return value


class ReviewModeRequest(BaseModel):
    enabled: bool = True


class OutgoingApproveRequest(BaseModel):
    ids: Optional[IdList] = Field(default=None, max_length=1000)  # None = approve everything waiting


class OutgoingDiscardRequest(BaseModel):
    ids: IdList = Field(min_length=1, max_length=1000)
    restore: bool = Field(default=True, description="false: only drop the rows, fetch nothing from the account "
                          "(the way out for rows that cannot be declined: there is no account to restore from)")


class IncomingResolveRequest(BaseModel):
    keys: Optional[List[str]] = Field(default=None, max_length=1000)
    accept: bool = True
