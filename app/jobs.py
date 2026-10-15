"""Durable sync jobs. Each step takes the current time so tests can control the clock.

Jobs run at least once: a worker can crash after writing but before finishing, so every
effect is idempotent. A worker writes only while it still holds the job's lease, so a
stalled worker whose job was recovered cannot overwrite the newer attempt's work.
"""
import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Review, ReviewSiteConnection, SyncAttempt, SyncJob, WorkerHeartbeat
from app.reviews import NormalizedReview, normalize_yelp
from app.yelp import BusinessNotFound, ProviderUnavailable, YelpClient

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Waits before attempts 2-5, each varied by +/-20% so failed jobs do not retry in lockstep.
BACKOFF_SECONDS = [30, 120, 480, 1800]
LEASE = timedelta(seconds=60)
PERMANENT_ERRORS = {"business_not_found"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def backoff(attempt: int, rng: Callable[[], float] = random.random) -> timedelta:
    base = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
    return timedelta(seconds=base * (0.8 + 0.4 * rng()))


def enqueue(session: Session, connection: ReviewSiteConnection, now: datetime) -> SyncJob:
    """Adds a queued job without committing. The unfinished-job index rejects a second one."""
    job = SyncJob(partner_id=connection.partner_id, connection_id=connection.id,
                  status="queued", attempts=0, max_attempts=MAX_ATTEMPTS, run_after=now,
                  created_at=now)
    session.add(job)
    return job


def claim(session: Session, worker_id: str, now: datetime) -> SyncJob | None:
    """Claims the oldest due job. SKIP LOCKED lets several workers claim different jobs."""
    job = session.scalar(select(SyncJob)
        .where(SyncJob.status == "queued", SyncJob.run_after <= now)
        .order_by(SyncJob.run_after, SyncJob.created_at).limit(1)
        .with_for_update(skip_locked=True))
    if job is None:
        session.rollback()
        return None
    job.status = "running"
    job.attempts += 1
    job.lease_owner = worker_id
    job.lease_expires_at = now + LEASE
    job.started_at = job.started_at or now
    session.add(SyncAttempt(job_id=job.id, number=job.attempts, worker_id=worker_id,
                            started_at=now, outcome="running"))
    session.commit()
    return job


def _write_reviews(session: Session, connection: ReviewSiteConnection,
                   reviews: list[NormalizedReview], now: datetime) -> dict:
    """Inserts new reviews and updates changed ones; unchanged reviews only advance
    last_seen_at, and reviews missing from this fetch are kept."""
    existing = {row.external_review_id: row for row in session.scalars(select(Review).where(
        Review.connection_id == connection.id,
        Review.external_review_id.in_([review.external_review_id for review in reviews]),
    ))}
    inserted = updated = 0
    for review in reviews:
        content = {"rating": review.rating, "text": review.text,
                   "author_name": review.author_name, "review_url": review.review_url,
                   "reviewed_at": review.reviewed_at, "content_hash": review.content_hash()}
        row = existing.get(review.external_review_id)
        if row is None:
            session.add(Review(partner_id=connection.partner_id, connection_id=connection.id,
                               external_review_id=review.external_review_id,
                               content_updated_at=now, last_seen_at=now, updated_at=now,
                               **content))
            inserted += 1
            continue
        row.last_seen_at = now
        if row.content_hash != content["content_hash"]:
            for key, value in content.items():
                setattr(row, key, value)
            row.content_updated_at = row.updated_at = now
            updated += 1
    return {"inserted": inserted, "updated": updated,
            "unchanged": len(reviews) - inserted - updated}


def _finish(job: SyncJob, attempt: SyncAttempt, status: str, outcome: str, now: datetime,
            error_code: str | None = None):
    job.status = status
    job.finished_at = now
    job.error_code = error_code
    job.lease_owner = job.lease_expires_at = None
    attempt.outcome = outcome
    attempt.finished_at = now
    attempt.error_code = error_code


def _retry_or_fail(job: SyncJob, attempt: SyncAttempt, connection: ReviewSiteConnection,
                   outcome: str, error_code: str, now: datetime):
    connection.last_sync_error = error_code
    if error_code in PERMANENT_ERRORS:
        _finish(job, attempt, "failed", "failed", now, error_code)
        connection.sync_status = "needs_attention"
        return
    connection.sync_status = "failing"
    if job.attempts >= job.max_attempts:
        _finish(job, attempt, "failed", "failed" if outcome == "retrying" else outcome, now,
                error_code)
        return
    job.status = "queued"
    job.run_after = now + backoff(job.attempts)
    job.error_code = error_code
    job.lease_owner = job.lease_expires_at = None
    attempt.outcome = outcome
    attempt.finished_at = now
    attempt.error_code = error_code


def run(session: Session, yelp: YelpClient, job: SyncJob, worker_id: str,
        clock: Callable[[], datetime] = utcnow) -> str:
    """Runs one claimed attempt. Returns the attempt's outcome, or `lease_lost`."""
    job_id, number = job.id, job.attempts
    connection_id = job.connection_id
    business_id = session.get(ReviewSiteConnection, connection_id).external_business_id
    # Outbound provider calls happen outside database transactions.
    session.commit()
    raw, error_code = None, None
    try:
        raw = yelp.reviews(business_id)
    except ProviderUnavailable:
        error_code = "provider_unavailable"
    except BusinessNotFound:
        error_code = "business_not_found"
    except Exception:
        log.exception("Unexpected provider error for job %s", job_id)
        error_code = "unexpected_error"
    reviews, rejected = normalize_yelp(raw) if raw is not None else ([], 0)

    job = session.scalar(select(SyncJob).where(SyncJob.id == job_id)
        .with_for_update().execution_options(populate_existing=True))
    if job.status != "running" or job.lease_owner != worker_id or job.attempts != number:
        session.rollback()
        log.warning("Lost the lease on job %s attempt %s; discarding its work", job_id, number)
        return "lease_lost"
    attempt = session.scalar(select(SyncAttempt).where(SyncAttempt.job_id == job_id,
                                                       SyncAttempt.number == number))
    # Disabling updates this row, so a disable during the provider call is seen here.
    connection = session.scalar(select(ReviewSiteConnection)
        .where(ReviewSiteConnection.id == connection_id)
        .with_for_update().execution_options(populate_existing=True))
    # Read the clock only once the locks are held, just before writing: review updated_at
    # then follows the order in which writes commit, as closely as one clock read allows.
    now = clock()
    if connection.disabled_at is not None:
        _finish(job, attempt, "cancelled", "cancelled", now, "connection_disabled")
    elif error_code is not None:
        _retry_or_fail(job, attempt, connection, "retrying", error_code, now)
    else:
        counts = _write_reviews(session, connection, reviews, now)
        for key, value in {**counts, "fetched": len(raw), "rejected": rejected}.items():
            setattr(job, key, value)
        _finish(job, attempt, "succeeded", "succeeded", now)
        connection.sync_status = "ok"
        connection.last_sync_error = None
        connection.last_synced_at = now
    outcome = attempt.outcome
    session.commit()
    return outcome


def recover_expired(session: Session, now: datetime) -> int:
    """Returns jobs whose worker vanished to the queue, or fails them after max attempts."""
    jobs = session.scalars(select(SyncJob)
        .where(SyncJob.status == "running", SyncJob.lease_expires_at < now)
        .with_for_update(skip_locked=True)).all()
    for job in jobs:
        attempt = session.scalar(select(SyncAttempt).where(SyncAttempt.job_id == job.id,
                                                           SyncAttempt.number == job.attempts))
        connection = session.get(ReviewSiteConnection, job.connection_id)
        if connection.disabled_at is not None:
            _finish(job, attempt, "cancelled", "lease_expired", now, "connection_disabled")
        else:
            _retry_or_fail(job, attempt, connection, "lease_expired", "lease_expired", now)
        log.warning("Recovered job %s after its lease expired", job.id)
    session.commit()
    return len(jobs)


def heartbeat(session: Session, worker_id: str, started_at: datetime, now: datetime):
    session.execute(insert(WorkerHeartbeat)
        .values(worker_id=worker_id, started_at=started_at, last_seen_at=now)
        .on_conflict_do_update(index_elements=["worker_id"], set_={"last_seen_at": now}))
    session.commit()


def cancel_queued(session: Session, connection_id: UUID, now: datetime):
    """Cancels queued jobs without committing. A running job cancels itself before writing.

    One conditional UPDATE, not select-then-write: PostgreSQL re-checks the status of a row
    a worker claimed meanwhile, so a just-claimed job is left to cancel itself."""
    session.execute(update(SyncJob)
        .where(SyncJob.connection_id == connection_id, SyncJob.status == "queued")
        .values(status="cancelled", finished_at=now, error_code="connection_disabled"))
