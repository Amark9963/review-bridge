import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Scope
from app.config import settings
from app.errors import ServiceError, not_found
from app.models import Account, Location, Partner, Review, ReviewSiteConnection
from app.reviews import normalize_yelp
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
        raise business_not_found() from None
    except ProviderUnavailable:
        raise provider_unavailable() from None
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


def connection_disabled():
    return ServiceError(409, "connection_disabled", "Connection is disabled")


def business_not_found():
    return ServiceError(422, "business_not_found",
                        "No business with this ID exists on the provider")


def provider_unavailable():
    return ServiceError(503, "provider_unavailable", "Provider is unavailable; retry later",
                        headers={"Retry-After": "30"})


def _claim_sync(session: Session, scope: Scope, connection_id: UUID, now: datetime):
    """Atomically claims the connection's sync slot, so concurrent or repeated requests
    cannot spend provider quota. Returns the previous claim so a failed call can undo it."""
    current = connection(session, scope, connection_id)
    if current.disabled_at is not None:
        raise connection_disabled()
    previous = current.sync_claimed_at
    cooldown = timedelta(seconds=settings().sync_cooldown_seconds)
    claimed = session.scalar(update(ReviewSiteConnection).where(
        ReviewSiteConnection.id == connection_id,
        ReviewSiteConnection.disabled_at.is_(None),
        or_(ReviewSiteConnection.sync_claimed_at.is_(None),
            ReviewSiteConnection.sync_claimed_at <= now - cooldown),
    ).values(sync_claimed_at=now).returning(ReviewSiteConnection.id))
    if claimed is None:
        session.rollback()
        session.refresh(current)
        if current.disabled_at is not None:
            raise connection_disabled()
        wait = cooldown - (now - current.sync_claimed_at)
        raise ServiceError(429, "sync_cooldown", "Connection was synced recently; retry later",
                           headers={"Retry-After": str(max(1, math.ceil(wait.total_seconds())))})
    session.commit()
    return current, previous


def sync_connection(session: Session, scope: Scope, yelp: YelpClient, connection_id: UUID):
    now = datetime.now(UTC)
    current, previous = _claim_sync(session, scope, connection_id, now)
    business_id = current.external_business_id
    # Outbound provider calls happen outside database transactions.
    try:
        raw = yelp.reviews(business_id)
    except ProviderUnavailable:
        # Give the slot back so the partner can retry once the provider recovers.
        session.execute(update(ReviewSiteConnection).where(
            ReviewSiteConnection.id == connection_id,
            ReviewSiteConnection.sync_claimed_at == now,
        ).values(sync_claimed_at=previous))
        session.commit()
        raise provider_unavailable() from None
    except BusinessNotFound:
        raise business_not_found() from None
    reviews, rejected = normalize_yelp(raw)

    # Disabling takes the same row lock, so a disable during the provider call is seen here
    # and nothing is written to a disabled connection.
    locked = session.scalar(select(ReviewSiteConnection)
        .where(ReviewSiteConnection.id == connection_id)
        .with_for_update().execution_options(populate_existing=True))
    if locked.disabled_at is not None:
        session.rollback()
        raise connection_disabled()
    existing = {row.external_review_id: row for row in session.scalars(select(Review).where(
        Review.connection_id == connection_id,
        Review.external_review_id.in_([review.external_review_id for review in reviews]),
    ))}
    inserted = updated = 0
    for review in reviews:
        content = {"rating": review.rating, "text": review.text,
                   "author_name": review.author_name, "review_url": review.review_url,
                   "reviewed_at": review.reviewed_at, "content_hash": review.content_hash()}
        row = existing.get(review.external_review_id)
        if row is None:
            session.add(Review(partner_id=locked.partner_id, connection_id=connection_id,
                               external_review_id=review.external_review_id,
                               content_updated_at=now, last_seen_at=now, **content))
            inserted += 1
            continue
        row.last_seen_at = now
        if row.content_hash != content["content_hash"]:
            for key, value in content.items():
                setattr(row, key, value)
            row.content_updated_at = now
            updated += 1
    locked.last_synced_at = now
    session.commit()
    return {"connection_id": connection_id, "synced_at": now, "fetched": len(raw),
            "inserted": inserted, "updated": updated,
            "unchanged": len(reviews) - inserted - updated, "rejected": rejected}


REVIEW_FIELDS = ["id", "connection_id", "external_review_id", "rating", "text", "author_name",
                 "review_url", "reviewed_at", "created_at", "content_updated_at"]


def review_feed(session: Session, scope: Scope, limit: int, page: int,
                account_id: UUID | None = None):
    """Reviews from active connections only, newest first."""
    query = (select(Review, ReviewSiteConnection.provider, ReviewSiteConnection.location_id,
                    Location.account_id)
        .join(ReviewSiteConnection, ReviewSiteConnection.id == Review.connection_id)
        .join(Location, Location.id == ReviewSiteConnection.location_id)
        .where(Review.partner_id == scope.partner_id,
               ReviewSiteConnection.disabled_at.is_(None)))
    if account_id is not None:
        account(session, scope, account_id)
        query = query.where(Location.account_id == account_id)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(query.order_by(Review.reviewed_at.desc(), Review.id)
        .limit(limit).offset((page - 1) * limit)).all()
    data = [{**{column: getattr(review, column) for column in REVIEW_FIELDS},
             "provider": provider, "location_id": location_id, "account_id": row_account_id}
            for review, provider, location_id, row_account_id in rows]
    return {"data": data, "meta": {"page": page, "limit": limit, "total": total,
                                   "last_page": max(1, math.ceil(total / limit))}}
