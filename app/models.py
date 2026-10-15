from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Partner(Identity, Base):
    __tablename__ = "partners"
    name: Mapped[str] = mapped_column(String(200))


class Account(Identity, Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("partner_id", "id", name="uq_accounts_partner_id_id"),)
    partner_id: Mapped[UUID] = mapped_column(ForeignKey("partners.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))


class Location(Identity, Base):
    __tablename__ = "locations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["partner_id", "account_id"], ["accounts.partner_id", "accounts.id"],
            name="fk_locations_account_tenant",
        ),
        UniqueConstraint("partner_id", "id", name="uq_locations_partner_id_id"),
    )
    partner_id: Mapped[UUID] = mapped_column(index=True)
    account_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200))


ACTIVE = text("disabled_at IS NULL")


class ReviewSiteConnection(Identity, Base):
    """A location's link to one external business, verified with the provider before it is
    stored. Never deleted, only disabled, because reviews will be keyed by connection.
    The external identity is immutable."""

    __tablename__ = "review_site_connections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["partner_id", "location_id"], ["locations.partner_id", "locations.id"],
            name="fk_connections_location_tenant",
        ),
        CheckConstraint("provider IN ('yelp')", name="ck_connections_provider"),
        CheckConstraint("disabled_reason IN ('incorrect_business', 'no_longer_needed')",
                        name="ck_connections_disabled_reason"),
        CheckConstraint("(disabled_at IS NULL) = (disabled_reason IS NULL)",
                        name="ck_connections_disabled_consistent"),
        Index("uq_connections_active_location", "location_id", "provider",
              unique=True, postgresql_where=ACTIVE),
        Index("uq_connections_active_business", "partner_id", "provider",
              "external_business_id", unique=True, postgresql_where=ACTIVE),
        UniqueConstraint("partner_id", "id", name="uq_connections_partner_id_id"),
        CheckConstraint("sync_status IN ('pending', 'ok', 'failing', 'needs_attention')",
                        name="ck_connections_sync_status"),
    )
    partner_id: Mapped[UUID]
    location_id: Mapped[UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_business_id: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String(32))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sync health, kept separate from disabling: failures never disable a connection.
    sync_status: Mapped[str] = mapped_column(String(32), default="pending",
                                             server_default="pending")
    last_sync_error: Mapped[str | None] = mapped_column(String(64))


class Review(Identity, Base):
    """Identity is (connection_id, external_review_id). Reviews stay with the connection
    that fetched them, and a review missing from a fetch is not a deletion."""

    __tablename__ = "reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["partner_id", "connection_id"],
            ["review_site_connections.partner_id", "review_site_connections.id"],
            name="fk_reviews_connection_tenant",
        ),
        UniqueConstraint("connection_id", "external_review_id",
                         name="uq_reviews_connection_external"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating"),
        Index("ix_reviews_partner_reviewed", "partner_id", "reviewed_at"),
        Index("ix_reviews_partner_updated", "partner_id", "updated_at", "id"),
    )
    partner_id: Mapped[UUID]
    connection_id: Mapped[UUID]
    external_review_id: Mapped[str] = mapped_column(String(64))
    rating: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str] = mapped_column(Text)
    author_name: Mapped[str] = mapped_column(String(200))
    review_url: Mapped[str] = mapped_column(String(2000))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    # Advances only when content changes; unchanged observations update last_seen_at.
    content_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # What feed clients sync on: advances when content changes or the review is removed
    # (its connection disabled), never for unchanged observations.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


UNFINISHED = text("status IN ('queued', 'running')")


class SyncJob(Identity, Base):
    """A requested sync, run at least once by a worker. Effects are idempotent, and at most
    one job per connection is unfinished at a time."""

    __tablename__ = "sync_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["partner_id", "connection_id"],
            ["review_site_connections.partner_id", "review_site_connections.id"],
            name="fk_sync_jobs_connection_tenant",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_sync_jobs_status"),
        CheckConstraint("trigger IN ('connect', 'manual', 'schedule')",
                        name="ck_sync_jobs_trigger"),
        Index("uq_sync_jobs_unfinished", "connection_id", unique=True,
              postgresql_where=UNFINISHED),
        Index("ix_sync_jobs_due", "run_after", postgresql_where=text("status = 'queued'")),
        Index("ix_sync_jobs_connection_created", "connection_id", "created_at"),
    )
    partner_id: Mapped[UUID]
    connection_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16))
    # Why the job exists: a new connection, a partner's request, or the schedule.
    trigger: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int]
    max_attempts: Mapped[int]
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The lease is a time-limited claim; an expired lease returns the job to the queue.
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    fetched: Mapped[int | None]
    inserted: Mapped[int | None]
    updated: Mapped[int | None]
    unchanged: Mapped[int | None]
    rejected: Mapped[int | None]


class SyncAttempt(Identity, Base):
    __tablename__ = "sync_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_sync_attempts_job_number"),
        CheckConstraint(
            "outcome IN ('running', 'succeeded', 'retrying', 'failed', 'cancelled', "
            "'lease_expired')", name="ck_sync_attempts_outcome"),
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("sync_jobs.id"))
    number: Mapped[int]
    worker_id: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApiToken(Identity, Base):
    """Only partner scope is issued in slice 1. Account scope arrives later."""

    __tablename__ = "api_tokens"
    partner_id: Mapped[UUID] = mapped_column(ForeignKey("partners.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
