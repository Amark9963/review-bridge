import base64
import json
import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import jobs
from app.auth import Scope, issue_token
from app.config import settings
from app.errors import ServiceError, not_found
from app.models import (
    Account,
    ApiToken,
    Location,
    Partner,
    Review,
    ReviewSiteConnection,
    SyncAttempt,
    SyncJob,
)
from app.schemas import ReviewFeedQuery, SyncJobOut
from app.yelp import BusinessNotFound, ProviderUnavailable, YelpClient

CONNECTION_CONFLICTS = {
    "uq_connections_active_location": (
        "connection_exists", "Location already has an active connection for this provider"),
    "uq_connections_active_business": (
        "business_already_connected",
        "Another location already has an active connection to this business"),
}


def require_partner(scope: Scope):
    """Partner-level endpoints refuse account tokens with 403: the caller is known, but the
    token type cannot use this endpoint. IDs outside an account token's account are 404."""
    if scope.account_id is not None:
        raise ServiceError(403, "insufficient_scope", "This endpoint needs a partner token")


def in_scope_locations(scope: Scope):
    """IDs of the locations a token may act on."""
    query = select(Location.id).where(Location.partner_id == scope.partner_id)
    if scope.account_id is not None:
        query = query.where(Location.account_id == scope.account_id)
    return query


def partner(session: Session, scope: Scope):
    require_partner(scope)
    result = session.get(Partner, scope.partner_id)
    if result is None:
        raise not_found()
    return result


def account(session: Session, scope: Scope, account_id: UUID):
    if scope.account_id is not None and account_id != scope.account_id:
        raise not_found()
    result = session.scalar(select(Account).where(
        Account.id == account_id, Account.partner_id == scope.partner_id,
    ))
    if result is None:
        raise not_found()
    return result


def location(session: Session, scope: Scope, location_id: UUID):
    result = session.scalar(select(Location).where(
        Location.id == location_id, Location.id.in_(in_scope_locations(scope)),
    ))
    if result is None:
        raise not_found()
    return result


def create_account(session: Session, scope: Scope, partner_id: UUID, name: str):
    require_partner(scope)
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
    require_partner(scope)
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
        ReviewSiteConnection.location_id.in_(in_scope_locations(scope)),
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
    now = datetime.now(UTC)
    result = ReviewSiteConnection(partner_id=scope.partner_id, location_id=location_id,
                                  provider=provider, external_business_id=external_business_id,
                                  verified_at=now)
    session.add(result)
    try:
        session.flush()
        # Queue the first collection with the connection, in the same transaction.
        jobs.enqueue(session, result, now, "connect")
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
    # Lock first, then read the clock: a worker writing reviews holds this lock, so the
    # removal's updated_at is always later than the worker's and never moves backwards.
    result = session.scalar(select(ReviewSiteConnection)
        .where(ReviewSiteConnection.id == connection_id)
        .with_for_update().execution_options(populate_existing=True))
    if result.disabled_at is None:
        now = datetime.now(UTC)
        result.disabled_at = now
        result.disabled_reason = reason
        session.flush()
        jobs.cancel_queued(session, connection_id, now)
        # Its reviews are now removed; advancing updated_at shows the removal to clients
        # syncing with updated_since. The worker re-checks this row before writing, so no
        # later sync can write to these reviews. GREATEST keeps updated_at from moving
        # backwards even if another host's clock ran ahead.
        session.execute(update(Review).where(Review.connection_id == connection_id)
                        .values(updated_at=func.greatest(Review.updated_at, now)))
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


def _unfinished_job(session: Session, connection_id: UUID):
    return session.scalar(select(SyncJob).where(SyncJob.connection_id == connection_id,
                                                SyncJob.status.in_(["queued", "running"])))


def request_sync(session: Session, scope: Scope, connection_id: UUID):
    """Queues a sync and returns its job. While one is unfinished, returns that job instead,
    so repeating the request is harmless. A cooldown since the last job protects quota."""
    current = connection(session, scope, connection_id)
    if current.disabled_at is not None:
        raise connection_disabled()
    existing = _unfinished_job(session, connection_id)
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    cooldown = timedelta(seconds=settings().sync_cooldown_seconds)
    latest = session.scalar(select(func.max(SyncJob.created_at))
        .where(SyncJob.connection_id == connection_id))
    if latest is not None and latest > now - cooldown:
        wait = math.ceil((latest + cooldown - now).total_seconds())
        raise ServiceError(429, "sync_cooldown", "Connection was synced recently; retry later",
                           headers={"Retry-After": str(max(1, wait))})
    job = jobs.enqueue(session, current, now, "manual")
    try:
        session.commit()
    except IntegrityError as error:
        # A concurrent request queued one first; return that job.
        session.rollback()
        if error.orig.diag.constraint_name != "uq_sync_jobs_unfinished":
            raise
        return _unfinished_job(session, connection_id)
    session.refresh(job)
    return job


def sync_job(session: Session, scope: Scope, job_id: UUID):
    connections = select(ReviewSiteConnection.id).where(
        ReviewSiteConnection.location_id.in_(in_scope_locations(scope)))
    result = session.scalar(select(SyncJob).where(SyncJob.id == job_id,
                                                  SyncJob.partner_id == scope.partner_id,
                                                  SyncJob.connection_id.in_(connections)))
    if result is None:
        raise not_found()
    attempts = session.scalars(select(SyncAttempt).where(SyncAttempt.job_id == job_id)
        .order_by(SyncAttempt.number)).all()
    return {**SyncJobOut.model_validate(result).model_dump(), "attempt_history": attempts}


def sync_jobs(session: Session, scope: Scope, connection_id: UUID, limit: int, page: int):
    connection(session, scope, connection_id)
    return session.scalars(select(SyncJob).where(
        SyncJob.partner_id == scope.partner_id, SyncJob.connection_id == connection_id,
    ).order_by(SyncJob.created_at.desc(), SyncJob.id)
        .limit(limit).offset((page - 1) * limit)).all()


REVIEW_FIELDS = ["id", "connection_id", "external_review_id", "rating", "text", "author_name",
                 "review_url", "reviewed_at", "created_at", "content_updated_at", "updated_at"]


def encode_cursor(updated_at: datetime, review_id: UUID) -> str:
    """Opaque to clients: the position after the last row served, as (updated_at, id)."""
    raw = json.dumps([updated_at.isoformat(), str(review_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        updated_at, review_id = json.loads(raw)
        position = datetime.fromisoformat(updated_at), UUID(review_id)
    except (ValueError, TypeError, AttributeError):
        position = None
    if position is None or position[0].tzinfo is None:
        raise ServiceError(422, "invalid_cursor",
                           "Cursor is malformed; restart the sync from updated_since")
    return position


def review_feed(session: Session, scope: Scope, q: ReviewFeedQuery,
                account_id: UUID | None = None):
    """Browsing (`sort=reviewed_at`) pages by number, newest first. Syncing
    (`sort=updated_at`) pages by cursor, oldest change first, so rows that change during a
    sync move to the end instead of shifting rows the client has not read yet."""
    query = (select(Review, ReviewSiteConnection.provider, ReviewSiteConnection.location_id,
                    Location.account_id, ReviewSiteConnection.disabled_at,
                    ReviewSiteConnection.disabled_reason)
        .join(ReviewSiteConnection, ReviewSiteConnection.id == Review.connection_id)
        .join(Location, Location.id == ReviewSiteConnection.location_id)
        .where(Review.partner_id == scope.partner_id))
    if not q.include_removed:
        query = query.where(ReviewSiteConnection.disabled_at.is_(None))
    if account_id is None:
        require_partner(scope)
    else:
        account(session, scope, account_id)
        query = query.where(Location.account_id == account_id)
    if q.location_id is not None:
        # Outside the partner, or outside the account being read: unknown, like any ID.
        found = location(session, scope, q.location_id)
        if account_id is not None and found.account_id != account_id:
            raise not_found()
        query = query.where(ReviewSiteConnection.location_id == q.location_id)
    filters = [
        (q.provider, lambda: ReviewSiteConnection.provider == q.provider),
        (q.rating, lambda: Review.rating.in_(q.rating)),
        (q.reviewed_from, lambda: Review.reviewed_at >= q.reviewed_from),
        (q.reviewed_to, lambda: Review.reviewed_at <= q.reviewed_to),
        (q.created_since, lambda: Review.created_at >= q.created_since),
        (q.updated_since, lambda: Review.updated_at >= q.updated_since),
    ]
    query = query.where(*[condition() for value, condition in filters if value is not None])

    if q.sort == "updated_at":
        if q.cursor is not None:
            query = query.where(tuple_(Review.updated_at, Review.id)
                                > tuple_(*decode_cursor(q.cursor)))
        rows = session.execute(query.order_by(Review.updated_at, Review.id)
                               .limit(q.limit + 1)).all()
        more, rows = len(rows) > q.limit, rows[:q.limit]
        last = rows[-1][0] if more else None
        meta = {"limit": q.limit,
                "next_cursor": encode_cursor(last.updated_at, last.id) if last else None}
    else:
        page = q.page or 1
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.execute(query.order_by(Review.reviewed_at.desc(), Review.id)
            .limit(q.limit).offset((page - 1) * q.limit)).all()
        meta = {"page": page, "limit": q.limit, "total": total,
                "last_page": max(1, math.ceil(total / q.limit))}
    data = [{**{column: getattr(review, column) for column in REVIEW_FIELDS},
             "provider": provider, "location_id": location_id, "account_id": row_account_id,
             "removed_at": disabled_at, "removed_reason": disabled_reason}
            for review, provider, location_id, row_account_id, disabled_at, disabled_reason
            in rows]
    return {"data": data, "meta": meta}


MAX_ACTIVE_ACCOUNT_TOKENS = 10


def current_token(session: Session, scope: Scope):
    return session.get(ApiToken, scope.token_id)


def create_account_token(session: Session, scope: Scope, account_id: UUID, label: str,
                         days: int):
    """Returns the stored token and its secret, which is shown once and never stored.
    Rate limits count per token, so the active tokens per account are capped; otherwise
    issuing more tokens would multiply an account's allowance."""
    require_partner(scope)
    account(session, scope, account_id)
    # Serializes concurrent issuance for this account, so the cap cannot be overrun.
    session.scalar(select(Account).where(Account.id == account_id).with_for_update())
    now = datetime.now(UTC)
    active = session.scalar(select(func.count()).select_from(ApiToken).where(
        ApiToken.account_id == account_id, ApiToken.revoked_at.is_(None),
        ApiToken.expires_at > now))
    if active >= MAX_ACTIVE_ACCOUNT_TOKENS:
        session.rollback()
        raise ServiceError(409, "token_limit",
                           f"An account can have at most {MAX_ACTIVE_ACCOUNT_TOKENS} active "
                           "tokens; revoke one first")
    token, secret = issue_token(session, scope.partner_id, label, days, account_id)
    session.commit()
    session.refresh(token)
    return token, secret


def account_tokens(session: Session, scope: Scope, account_id: UUID, limit: int, page: int):
    """Every token ever issued for the account, newest first, including revoked ones."""
    require_partner(scope)
    account(session, scope, account_id)
    return session.scalars(select(ApiToken).where(ApiToken.account_id == account_id)
        .order_by(ApiToken.created_at.desc(), ApiToken.id)
        .limit(limit).offset((page - 1) * limit)).all()


def revoke_account_token(session: Session, scope: Scope, token_id: UUID):
    """Partners revoke their accounts' tokens; partner tokens are revoked by the operator.
    Repeating the call returns the token unchanged."""
    require_partner(scope)
    token = session.scalar(select(ApiToken).where(ApiToken.id == token_id,
                                                  ApiToken.partner_id == scope.partner_id,
                                                  ApiToken.account_id.is_not(None)))
    if token is None:
        raise not_found()
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        session.commit()
        session.refresh(token)
    return token
