from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint, func
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
    )
    partner_id: Mapped[UUID] = mapped_column(index=True)
    account_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200))


class ApiToken(Identity, Base):
    """Only partner scope is issued in slice 1. Account scope arrives later."""

    __tablename__ = "api_tokens"
    partner_id: Mapped[UUID] = mapped_column(ForeignKey("partners.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
