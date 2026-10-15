from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


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
    trigger: Literal["connect", "manual", "schedule"]
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
    updated_at: datetime
    # Set only with include_removed, for reviews whose connection was disabled.
    removed_at: datetime | None
    removed_reason: DisableReason | None


# Deep offsets get slow and are never needed: filter, or sync with sort=updated_at.
MAX_OFFSET = 10_000


class ReviewFeedQuery(BaseModel):
    """Two orders. `reviewed_at` (default, newest first) is for browsing, with page/limit.
    `updated_at` (oldest change first) is for incremental sync, with cursors: rows that
    change mid-sync move to the end instead of shifting unread pages."""

    model_config = ConfigDict(extra="forbid")
    limit: Annotated[int, Field(ge=1, le=100)] = 25
    page: Annotated[int, Field(ge=1)] | None = None
    sort: Literal["reviewed_at", "updated_at"] = "reviewed_at"
    cursor: Annotated[str, StringConstraints(max_length=200)] | None = None
    location_id: UUID | None = None
    provider: Provider | None = None
    rating: list[Annotated[int, Field(ge=1, le=5)]] | None = None
    # Timestamps need an explicit offset (for example `Z`), so they are never ambiguous.
    reviewed_from: AwareDatetime | None = None
    reviewed_to: AwareDatetime | None = None
    created_since: AwareDatetime | None = None
    updated_since: AwareDatetime | None = None
    include_removed: bool = False

    @model_validator(mode="after")
    def consistent(self):
        if self.sort == "updated_at" and self.page is not None:
            raise ValueError("page is not used with sort=updated_at; follow meta.next_cursor")
        if self.cursor is not None and self.sort != "updated_at":
            raise ValueError("cursor requires sort=updated_at")
        if self.reviewed_from and self.reviewed_to and self.reviewed_from > self.reviewed_to:
            raise ValueError("reviewed_from must not be after reviewed_to")
        if ((self.page or 1) - 1) * self.limit >= MAX_OFFSET:
            raise ValueError(f"pages beyond {MAX_OFFSET} rows are not served; "
                             "narrow the filters or use sort=updated_at")
        return self


class PageMeta(BaseModel):
    page: int
    limit: int
    total: int
    last_page: int


class CursorMeta(BaseModel):
    limit: int
    # Null once there are no more rows; start the next sync from updated_since instead.
    next_cursor: str | None


class ReviewPageOut(BaseModel):
    data: list[ReviewOut]
    meta: PageMeta | CursorMeta


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


class CreateAccountToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                            max_length=200)]
    expires_in_days: Annotated[int, Field(ge=1, le=365)] = 90


class TokenOut(BaseModel):
    """Never includes the secret or its hash; `prefix` identifies a token in conversation."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    partner_id: UUID
    account_id: UUID | None
    label: str
    prefix: str
    expires_at: datetime
    revoked_at: datetime | None


class CreatedTokenOut(TokenOut):
    # Shown once. Store it securely; it cannot be retrieved again.
    token: str
