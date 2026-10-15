from datetime import timedelta

from sqlalchemy import select
from test_connections import business, disable
from test_reviews import (  # noqa: F401
    FakeYelp,
    connected,
    latest_job,
    no_cooldown,
    sync,
    work,
)

from app import jobs
from app.cli import overdue
from app.models import SyncJob
from app.yelp import BusinessNotFound

INTERVAL = timedelta(hours=12)


class UnknownBusiness(FakeYelp):
    """Yelp no longer knows one business; the others sync normally."""

    def __init__(self, missing):
        super().__init__()
        self.missing = missing

    def reviews(self, business_id):
        if business_id == self.missing:
            raise BusinessNotFound()
        return super().reviews(business_id)


def scheduled(session, connection_id):
    return session.scalars(select(SyncJob).where(SyncJob.connection_id == connection_id,
                                                 SyncJob.trigger == "schedule")
                           .order_by(SyncJob.created_at)).all()


def test_triggers_are_recorded(client, session, tenants):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    assert latest_job(client, a, connection_id)["trigger"] == "connect"
    work(session, FakeYelp())
    assert sync(client, a, connection_id).json()["trigger"] == "manual"


def test_schedules_only_due_active_healthy_connections(client, session, tenants):
    a = tenants[0]
    start = jobs.utcnow()
    healthy = connected(client, a, n=1)["id"]
    disabled = connected(client, a, n=2)["id"]
    broken = connected(client, a, n=3)["id"]
    work(session, UnknownBusiness(business(3)))
    disable(client, a, disabled, "no_longer_needed")
    assert client.get(f"/v1/connections/{broken}",
                      headers={"Authorization": f"Bearer {a[2]}"}).json()["sync_status"] == (
        "needs_attention")
    unfinished = connected(client, a, n=4)["id"]  # Its first job is queued, not yet run.

    later = start + INTERVAL + timedelta(hours=1)
    assert jobs.schedule_due(session, later, INTERVAL) == 1
    job = scheduled(session, healthy)[0]
    assert job.status == "queued" and job.attempts == 0
    assert later <= job.run_after <= later + jobs.SCHEDULE_SPREAD
    for connection_id in [disabled, broken, unfinished]:
        assert scheduled(session, connection_id) == []

    # Repeating finds nothing new: the queued job is unfinished.
    assert jobs.schedule_due(session, later, INTERVAL) == 0
    # Runs the scheduled job and the unfinished connection's first job.
    assert work(session, FakeYelp(), later + jobs.SCHEDULE_SPREAD) == ["succeeded"] * 2
    # An hour on, `healthy` synced within the interval and is not due; `unfinished` has
    # had no job since it was connected, so it is.
    assert jobs.schedule_due(session, later + timedelta(hours=1), INTERVAL) == 1
    assert len(scheduled(session, healthy)) == 1 and len(scheduled(session, unfinished)) == 1
    assert jobs.schedule_due(session, later + INTERVAL + timedelta(hours=1), INTERVAL) == 1
    assert len(scheduled(session, healthy)) == 2


def test_successful_manual_sync_returns_a_connection_to_the_schedule(client, session,
                                                                     tenants):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    start = jobs.utcnow()
    work(session, UnknownBusiness(business(1)), start)
    later = start + INTERVAL + timedelta(hours=1)
    assert jobs.schedule_due(session, later, INTERVAL) == 0
    # The partner fixed nothing upstream, but Yelp knows the business again.
    sync(client, a, connection_id)
    work(session, FakeYelp(), jobs.utcnow())
    assert jobs.schedule_due(session, jobs.utcnow() + INTERVAL + timedelta(hours=1),
                             INTERVAL) == 1


def test_overdue_connections(client, session, tenants):
    a = tenants[0]
    connected(client, a)
    now = jobs.utcnow()
    assert overdue(session, now) == 0
    # An hour past the interval with no new job: the scheduler is not keeping up.
    assert overdue(session, now + INTERVAL + timedelta(hours=2)) == 1
