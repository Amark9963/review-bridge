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
    last_synced_at: datetime | None
    sync_status: Literal["pending", "ok", "failing", "needs_attention"]
    last_sync_error: str | None


class SyncJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    connection_id: UUID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    max_attempts: int
    run_after: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    fetched: int | None
    inserted: int | None
    updated: int | None
    unchanged: int | None
    rejected: int | None


class SyncAttemptOut(BaseModel):
    """Worker identities are operational detail and are not shown to partners."""

    model_config = ConfigDict(from_attributes=True)
    number: int
    started_at: datetime
    finished_at: datetime | None
    outcome: Literal["running", "succeeded", "retrying", "failed", "cancelled",
                     "lease_expired"]
    error_code: str | None


class SyncJobDetailOut(SyncJobOut):
    attempt_history: list[SyncAttemptOut]


class ReviewOut(BaseModel):
    id: UUID
    connection_id: UUID
    location_id: UUID
    account_id: UUID
    provider: Provider
    external_review_id: str
    rating: int
    text: str
    author_name: str
    review_url: str
    reviewed_at: datetime
    created_at: datetime
    content_updated_at: datetime


class PageMeta(BaseModel):
    page: int
    limit: int
    total: int
    last_page: int


class ReviewPageOut(BaseModel):
    data: list[ReviewOut]
    meta: PageMeta


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
    excerpt_max_chars: int
    full_history: bool
    deletion_detection: bool
    reply_posting: bool
    content_max_age_hours: int
