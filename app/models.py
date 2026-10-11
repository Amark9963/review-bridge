from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
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
    )
    partner_id: Mapped[UUID]
    location_id: Mapped[UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_business_id: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String(32))


class ApiToken(Identity, Base):
    """Only partner scope is issued in slice 1. Account scope arrives later."""

    __tablename__ = "api_tokens"
    partner_id: Mapped[UUID] = mapped_column(ForeignKey("partners.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
