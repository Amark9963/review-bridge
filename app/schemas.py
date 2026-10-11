from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints


class CreateNamed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class PartnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    created_at: datetime


class AccountOut(PartnerOut):
    partner_id: UUID


class LocationOut(AccountOut):
    account_id: UUID


Provider = Literal["yelp"]
DisableReason = Literal["incorrect_business", "no_longer_needed"]


class CreateConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    # Yelp business IDs are 22 URL-safe characters. Aliases are not accepted.
    external_business_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{22}$")]


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    partner_id: UUID
    location_id: UUID
    provider: Provider
    external_business_id: str
    verified_at: datetime
    disabled_at: datetime | None
    disabled_reason: DisableReason | None


class BusinessOut(BaseModel):
    """Provider details shown once for confirmation and never stored, pending Yelp's
    caching terms."""

    model_config = ConfigDict(from_attributes=True)
    name: str
    address: str
    url: str


class CreatedConnectionOut(ConnectionOut):
    business: BusinessOut


class DisableConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: DisableReason


class ReviewSiteOut(BaseModel):
    provider: Provider
    reviews: Literal["sample"]
    max_reviews_per_fetch: int
    full_history: bool
    deletion_detection: bool
    reply_posting: bool
    content_max_age_hours: int
