from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Scope
from app.errors import ServiceError, not_found
from app.models import Account, Location, Partner, ReviewSiteConnection
from app.yelp import BusinessNotFound, ProviderUnavailable, YelpClient

CONNECTION_CONFLICTS = {
    "uq_connections_active_location": (
        "connection_exists", "Location already has an active connection for this provider"),
    "uq_connections_active_business": (
        "business_already_connected",
        "Another location already has an active connection to this business"),
}


def partner(session: Session, scope: Scope):
    result = session.get(Partner, scope.partner_id)
    if result is None:
        raise not_found()
    return result


def account(session: Session, scope: Scope, account_id: UUID):
    result = session.scalar(select(Account).where(
        Account.id == account_id, Account.partner_id == scope.partner_id,
    ))
    if result is None:
        raise not_found()
    return result


def location(session: Session, scope: Scope, location_id: UUID):
    result = session.scalar(select(Location).where(
        Location.id == location_id, Location.partner_id == scope.partner_id,
    ))
    if result is None:
        raise not_found()
    return result


def create_account(session: Session, scope: Scope, partner_id: UUID, name: str):
    if partner_id != scope.partner_id:
        raise not_found()
    result = Account(partner_id=scope.partner_id, name=name)
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def create_location(session: Session, scope: Scope, account_id: UUID, name: str):
    account(session, scope, account_id)
    result = Location(partner_id=scope.partner_id, account_id=account_id, name=name)
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def accounts(session: Session, scope: Scope, limit: int, page: int):
    return session.scalars(select(Account).where(Account.partner_id == scope.partner_id)
        .order_by(Account.created_at, Account.id).limit(limit).offset((page - 1) * limit)).all()


def locations(session: Session, scope: Scope, account_id: UUID, limit: int, page: int):
    account(session, scope, account_id)
    return session.scalars(select(Location).where(
        Location.partner_id == scope.partner_id, Location.account_id == account_id,
    ).order_by(Location.created_at, Location.id).limit(limit).offset((page - 1) * limit)).all()


def connection(session: Session, scope: Scope, connection_id: UUID):
    result = session.scalar(select(ReviewSiteConnection).where(
        ReviewSiteConnection.id == connection_id,
        ReviewSiteConnection.partner_id == scope.partner_id,
    ))
    if result is None:
        raise not_found()
    return result


def create_connection(session: Session, scope: Scope, yelp: YelpClient, location_id: UUID,
                      provider: str, external_business_id: str):
    """Returns the stored connection and the provider's business details, which are shown
    for confirmation but never stored."""
    location(session, scope, location_id)
    # Check conflicts first so requests that would fail anyway do not spend provider quota.
    existing = session.scalars(select(ReviewSiteConnection).where(
        ReviewSiteConnection.partner_id == scope.partner_id,
        ReviewSiteConnection.provider == provider,
        ReviewSiteConnection.disabled_at.is_(None),
        (ReviewSiteConnection.location_id == location_id)
        | (ReviewSiteConnection.external_business_id == external_business_id),
    )).all()
    if existing:
        key = ("uq_connections_active_location"
               if any(row.location_id == location_id for row in existing)
               else "uq_connections_active_business")
        raise ServiceError(409, *CONNECTION_CONFLICTS[key])
    # Outbound provider calls happen outside database transactions.
    session.commit()
    try:
        business = yelp.lookup(external_business_id)
    except BusinessNotFound:
        raise ServiceError(422, "business_not_found",
                           "No business with this ID exists on the provider") from None
    except ProviderUnavailable:
        raise ServiceError(503, "provider_unavailable", "Provider is unavailable; retry later",
                           headers={"Retry-After": "30"}) from None
    result = ReviewSiteConnection(partner_id=scope.partner_id, location_id=location_id,
                                  provider=provider, external_business_id=external_business_id,
                                  verified_at=datetime.now(UTC))
    session.add(result)
    try:
        session.commit()
    except IntegrityError as error:
        # The partial unique indexes are the source of truth, so concurrent requests
        # cannot both succeed.
        session.rollback()
        conflict = CONNECTION_CONFLICTS.get(error.orig.diag.constraint_name)
        if conflict is None:
            raise
        raise ServiceError(409, *conflict) from None
    session.refresh(result)
    return result, business


def connections(session: Session, scope: Scope, location_id: UUID, include_disabled: bool,
                limit: int, page: int):
    location(session, scope, location_id)
    query = select(ReviewSiteConnection).where(
        ReviewSiteConnection.partner_id == scope.partner_id,
        ReviewSiteConnection.location_id == location_id,
    )
    if not include_disabled:
        query = query.where(ReviewSiteConnection.disabled_at.is_(None))
    order = (ReviewSiteConnection.created_at, ReviewSiteConnection.id)
    return session.scalars(query.order_by(*order).limit(limit).offset((page - 1) * limit)).all()


def disable_connection(session: Session, scope: Scope, connection_id: UUID, reason: str):
    """Repeating the call returns the connection unchanged, keeping the original reason."""
    result = connection(session, scope, connection_id)
    if result.disabled_at is None:
        result.disabled_at = datetime.now(UTC)
        result.disabled_reason = reason
        session.commit()
        session.refresh(result)
    return result
