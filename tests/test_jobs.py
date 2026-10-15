import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_connections import headers
from test_reviews import WORKER, FakeYelp, connected, latest_job, work

from app import jobs
from app.cli import job_stats
from app.db import engine
from app.models import (
    Account,
    Location,
    Partner,
    Review,
    ReviewSiteConnection,
    SyncAttempt,
    SyncJob,
    WorkerHeartbeat,
)
from app.yelp import BusinessNotFound, ProviderUnavailable


@pytest.fixture
def yelp(client):
    return FakeYelp()


def job_detail(client, tenant, job_id):
    return client.get(f"/v1/sync-jobs/{job_id}", headers=headers(tenant)).json()


def get_connection(client, tenant, connection_id):
    return client.get(f"/v1/connections/{connection_id}", headers=headers(tenant)).json()


def test_first_sync_is_queued_on_connect(client, tenants):
    a = tenants[0]
    connection = connected(client, a)
    assert connection["sync_status"] == "pending"
    job = latest_job(client, a, connection["id"])
    assert (job["status"], job["attempts"], job["max_attempts"]) == ("queued", 0, 5)


def test_backoff_grows_with_jitter():
    for attempt, base in [(1, 30), (2, 120), (3, 480), (4, 1800), (9, 1800)]:
        assert jobs.backoff(attempt, rng=lambda: 0) == timedelta(seconds=base * 0.8)
        assert jobs.backoff(attempt, rng=lambda: 1) == timedelta(seconds=base * 1.2)


def test_outages_retry_with_backoff_then_fail(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    yelp.error = ProviderUnavailable()
    now = jobs.utcnow()
    assert work(session, yelp, now) == ["retrying"]
    job = latest_job(client, a, connection_id)
    assert (job["status"], job["attempts"], job["error_code"]) == (
        "queued", 1, "provider_unavailable")
    run_after = datetime.fromisoformat(job["run_after"])
    assert now + timedelta(seconds=24) <= run_after <= now + timedelta(seconds=36)
    connection = get_connection(client, a, connection_id)
    assert (connection["sync_status"], connection["last_sync_error"]) == (
        "failing", "provider_unavailable")
    assert work(session, yelp, now + timedelta(seconds=10)) == []

    for _ in range(4):
        now += timedelta(hours=1)
        work(session, yelp, now)
    detail = job_detail(client, a, job["id"])
    assert (detail["status"], detail["attempts"]) == ("failed", 5)
    assert [row["outcome"] for row in detail["attempt_history"]] == ["retrying"] * 4 + ["failed"]
    assert "worker_id" not in str(detail)
    assert get_connection(client, a, connection_id)["disabled_at"] is None


def test_recovery_after_failure_marks_connection_ok(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    yelp.error = ProviderUnavailable()
    now = jobs.utcnow()
    work(session, yelp, now)
    yelp.error = None
    assert work(session, yelp, now + timedelta(hours=1)) == ["succeeded"]
    connection = get_connection(client, a, connection_id)
    assert (connection["sync_status"], connection["last_sync_error"]) == ("ok", None)
    assert latest_job(client, a, connection_id)["inserted"] == 3


@pytest.mark.parametrize("error, status, code, sync_status", [
    (BusinessNotFound(), "failed", "business_not_found", "needs_attention"),
    (RuntimeError("bug"), "queued", "unexpected_error", "failing"),
])
def test_error_classification(client, session, tenants, yelp, error, status, code,
                              sync_status):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    yelp.error = error
    work(session, yelp)
    job = latest_job(client, a, connection_id)
    assert (job["status"], job["error_code"]) == (status, code)
    connection = get_connection(client, a, connection_id)
    assert connection["sync_status"] == sync_status
    # Failures never disable a connection; a person decides.
    assert connection["disabled_at"] is None


def test_expired_lease_returns_job_to_queue(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    now = jobs.utcnow()
    assert jobs.claim(session, "crashed-worker", now) is not None
    assert jobs.recover_expired(session, now + timedelta(seconds=30)) == 0
    assert jobs.recover_expired(session, now + timedelta(seconds=61)) == 1
    job = latest_job(client, a, connection_id)
    assert (job["status"], job["error_code"]) == ("queued", "lease_expired")
    assert work(session, yelp, now + timedelta(hours=1)) == ["succeeded"]
    history = job_detail(client, a, job["id"])["attempt_history"]
    assert [row["outcome"] for row in history] == ["lease_expired", "succeeded"]


def test_worker_that_lost_its_lease_writes_nothing(client, session, tenants, yelp):
    a = tenants[0]
    connected(client, a)
    now = jobs.utcnow()
    stalled = jobs.claim(session, "stalled-worker", now)
    later = now + timedelta(minutes=5)
    jobs.recover_expired(session, later)
    retried = jobs.claim(session, WORKER, later + timedelta(hours=1))
    assert retried is not None
    assert jobs.run(session, yelp, stalled, "stalled-worker") == "lease_lost"
    assert session.scalars(select(Review)).all() == []
    assert jobs.run(session, yelp, retried, WORKER) == "succeeded"


def test_repeated_lease_expiry_eventually_fails(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    now = jobs.utcnow()
    for _ in range(5):
        now += timedelta(hours=1)
        assert jobs.claim(session, "crashing-worker", now) is not None
        now += timedelta(minutes=2)
        jobs.recover_expired(session, now)
    job = latest_job(client, a, connection_id)
    assert (job["status"], job["attempts"], job["error_code"]) == ("failed", 5, "lease_expired")


def test_database_allows_one_unfinished_job_per_connection(client, session, tenants):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    existing = session.scalar(select(SyncJob).where(SyncJob.connection_id == connection_id))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(SyncJob(partner_id=existing.partner_id, connection_id=connection_id,
                            status="queued", trigger="manual", attempts=0, max_attempts=5,
                            run_after=jobs.utcnow()))
        session.flush()


@pytest.fixture
def committed_job(session):
    """A queued job committed for real, so two connections can race over it. The usual
    fixtures share one rolled-back transaction, which cannot show concurrency."""
    with Session(engine()) as setup:
        partner = Partner(name="Race")
        setup.add(partner)
        setup.flush()
        account = Account(partner_id=partner.id, name="Race")
        setup.add(account)
        setup.flush()
        location = Location(partner_id=partner.id, account_id=account.id, name="Race")
        setup.add(location)
        setup.flush()
        connection = ReviewSiteConnection(
            partner_id=partner.id, location_id=location.id, provider="yelp",
            external_business_id="rbtest0000000000000099", verified_at=jobs.utcnow())
        setup.add(connection)
        setup.flush()
        job = jobs.enqueue(setup, connection, jobs.utcnow(), "connect")
        setup.commit()
        ids = partner.id, connection.id, job.id
    yield ids
    partner_id, connection_id, _ = ids
    with Session(engine()) as cleanup:
        job_ids = select(SyncJob.id).where(SyncJob.connection_id == connection_id)
        cleanup.execute(delete(SyncAttempt).where(SyncAttempt.job_id.in_(job_ids)))
        for model in [SyncJob, ReviewSiteConnection, Location, Account]:
            cleanup.execute(delete(model).where(model.partner_id == partner_id))
        cleanup.execute(delete(Partner).where(Partner.id == partner_id))
        cleanup.commit()


def test_disable_does_not_cancel_a_job_claimed_meanwhile(committed_job):
    _, connection_id, job_id = committed_job
    with Session(engine()) as worker, Session(engine()) as api:
        # A worker claims the job but has not committed yet.
        claimed = worker.scalar(select(SyncJob).where(SyncJob.id == job_id).with_for_update())
        claimed.status = "running"
        worker.flush()

        def disable():
            jobs.cancel_queued(api, connection_id, jobs.utcnow())
            api.commit()

        thread = threading.Thread(target=disable)
        thread.start()
        with Session(engine()) as probe:
            for _ in range(100):
                if probe.scalar(text("SELECT count(*) FROM pg_stat_activity "
                                     "WHERE wait_event_type = 'Lock'")):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("cancel_queued never waited for the worker's row lock")
        worker.commit()
        thread.join(timeout=10)
    with Session(engine()) as check:
        assert check.get(SyncJob, job_id).status == "running"


def test_heartbeat_and_job_stats(client, session, tenants, yelp):
    a = tenants[0]
    connected(client, a, n=1)
    connected(client, a, n=2)
    now = jobs.utcnow()
    jobs.heartbeat(session, "worker-1", now, now)
    jobs.heartbeat(session, "worker-1", now, now + timedelta(seconds=5))
    beat = session.get(WorkerHeartbeat, "worker-1")
    session.refresh(beat)
    assert beat.last_seen_at == now + timedelta(seconds=5)
    stats = job_stats(session)
    assert (stats["queued_due"], stats["running"], stats["workers_alive"]) == (2, 0, 1)
    work(session, yelp)
    stats = job_stats(session)
    assert (stats["queued_due"], stats["succeeded_24h"]) == (0, 2)
