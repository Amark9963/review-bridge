import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from test_connections import connect, headers, new_location

from app import jobs
from app.config import settings
from app.main import app
from app.models import Review, ReviewSiteConnection
from app.yelp import SyntheticYelp, yelp_client

WORKER = "test-worker"


def raw(n, rating=5, text="Great visit.", name="Jane D.", created="2026-03-01 10:00:00"):
    return {"id": f"review{n}", "url": f"https://yelp.invalid/biz/x?hrid=review{n}",
            "text": text, "rating": rating, "time_created": created,
            "user": {"id": f"user{n}", "profile_url": f"https://yelp.invalid/user/{n}",
                     "image_url": "https://yelp.invalid/photo.jpg", "name": name}}


class FakeYelp(SyntheticYelp):
    def __init__(self):
        self.raw = None
        self.error = None
        self.during_fetch = None

    def reviews(self, business_id):
        if self.during_fetch:
            self.during_fetch()
        if self.error:
            raise self.error
        if self.raw is None:
            return super().reviews(business_id)
        return copy.deepcopy(self.raw)


@pytest.fixture
def yelp(client):
    fake = FakeYelp()
    app.dependency_overrides[yelp_client] = lambda: fake
    return fake


@pytest.fixture(autouse=True)
def no_cooldown(monkeypatch):
    monkeypatch.setattr(settings(), "sync_cooldown_seconds", 0)


def work(session, yelp, now=None):
    """Runs every due job, as the worker would, and returns the attempt outcomes."""
    now = now or jobs.utcnow()
    outcomes = []
    while (job := jobs.claim(session, WORKER, now)) is not None:
        outcomes.append(jobs.run(session, yelp, job, WORKER, clock=lambda: now))
    return outcomes


def connected(client, tenant, n=1):
    """Connects a new location; its first sync is queued but not yet run."""
    return connect(client, tenant, new_location(client, tenant), n=n).json()


def sync(client, tenant, connection_id):
    return client.post(f"/v1/connections/{connection_id}/sync", headers=headers(tenant))


def latest_job(client, tenant, connection_id):
    return client.get(f"/v1/connections/{connection_id}/sync-jobs",
                      headers=headers(tenant)).json()[0]


def counts(job):
    return {key: job[key] for key in ["fetched", "inserted", "updated", "unchanged", "rejected"]}


def feed(client, tenant, path="/v1/partner/reviews"):
    return client.get(path, headers=headers(tenant)).json()


def stored(session, external_review_id):
    session.expire_all()
    return session.scalar(select(Review).where(Review.external_review_id == external_review_id))


def test_sync_inserts_then_leaves_unchanged_reviews_alone(client, session, tenants, yelp):
    a = tenants[0]
    connection = connected(client, a)
    assert work(session, yelp) == ["succeeded"]
    assert counts(latest_job(client, a, connection["id"])) == {
        "fetched": 3, "inserted": 3, "updated": 0, "unchanged": 0, "rejected": 0}
    body = feed(client, a)
    assert body["meta"] == {"page": 1, "limit": 25, "total": 3, "last_page": 1}
    newest = body["data"][0]
    assert newest["rating"] == 5 and newest["author_name"] == "Sam K."
    assert newest["provider"] == "yelp"
    assert newest["connection_id"] == connection["id"]
    assert newest["location_id"] == connection["location_id"]
    assert "profile_url" not in str(body) and "image_url" not in str(body)
    before = stored(session, newest["external_review_id"])
    content_updated_at, last_seen_at = before.content_updated_at, before.last_seen_at

    assert sync(client, a, connection["id"]).status_code == 202
    work(session, yelp)
    job = latest_job(client, a, connection["id"])
    assert (job["inserted"], job["updated"], job["unchanged"]) == (0, 0, 3)
    after = stored(session, newest["external_review_id"])
    assert after.content_updated_at == content_updated_at
    assert after.last_seen_at > last_seen_at
    connection = client.get(f"/v1/connections/{connection['id']}", headers=headers(a)).json()
    assert connection["sync_status"] == "ok" and connection["last_sync_error"] is None
    assert connection["last_synced_at"] == job["finished_at"]


def test_edited_review_updates_content_and_timestamp(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    yelp.raw = [raw(1), raw(2)]
    work(session, yelp)
    original = stored(session, "review1").content_updated_at
    untouched = stored(session, "review2").content_updated_at
    yelp.raw = [raw(1, rating=3, text="Updated: billing was slow."), raw(2)]
    sync(client, a, connection_id)
    work(session, yelp)
    job = latest_job(client, a, connection_id)
    assert (job["inserted"], job["updated"], job["unchanged"]) == (0, 1, 1)
    edited = stored(session, "review1")
    assert edited.rating == 3 and edited.text == "Updated: billing was slow."
    assert edited.content_updated_at > original
    assert stored(session, "review2").content_updated_at == untouched


def test_missing_review_is_not_a_deletion(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    yelp.raw = [raw(1), raw(2)]
    work(session, yelp)
    yelp.raw = [raw(1)]
    sync(client, a, connection_id)
    work(session, yelp)
    job = latest_job(client, a, connection_id)
    assert (job["fetched"], job["unchanged"]) == (1, 1)
    assert feed(client, a)["meta"]["total"] == 2


def test_bad_entries_are_rejected_individually(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    no_id = raw(10)
    del no_id["id"]
    no_user = raw(11)
    del no_user["user"]
    yelp.raw = [
        raw(1), raw(2, text="x" * 200), no_id, no_user,
        raw(3, rating=6), raw(4, rating=4.5), raw(5, rating=True), raw(6, rating="5"),
        raw(7, text="   "), raw(8, created="29/08/2016"), raw(9, name=""),
        {**raw(12), "url": "http://yelp.invalid/insecure"},
        raw(1, text="duplicate of the first"), "not an object",
    ]
    work(session, yelp)
    job = latest_job(client, a, connection_id)
    assert (job["fetched"], job["inserted"], job["rejected"]) == (14, 2, 12)
    assert stored(session, "review1").text == "Great visit."
    assert len(stored(session, "review2").text) == 160


def test_sync_request_returns_the_unfinished_job(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    first = latest_job(client, a, connection_id)
    assert first["status"] == "queued"
    for _ in range(2):
        response = sync(client, a, connection_id)
        assert response.status_code == 202
        assert response.json()["id"] == first["id"]
    work(session, yelp)
    response = sync(client, a, connection_id)
    assert response.status_code == 202
    assert response.json()["id"] != first["id"] and response.json()["status"] == "queued"


def test_sync_refuses_disabled_connection(client, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    client.post(f"/v1/connections/{connection_id}/disable", headers=headers(a),
                json={"reason": "no_longer_needed"})
    assert latest_job(client, a, connection_id)["status"] == "cancelled"
    response = sync(client, a, connection_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "connection_disabled"


def test_disable_during_provider_call_writes_nothing(client, session, tenants, yelp):
    a = tenants[0]
    connection_id = connected(client, a)["id"]

    def disable_now():
        session.execute(update(ReviewSiteConnection)
            .where(ReviewSiteConnection.id == connection_id)
            .values(disabled_at=datetime.now(UTC), disabled_reason="incorrect_business"))
        session.commit()

    yelp.during_fetch = disable_now
    assert work(session, yelp) == ["cancelled"]
    job = latest_job(client, a, connection_id)
    assert (job["status"], job["error_code"]) == ("cancelled", "connection_disabled")
    assert session.scalar(select(func.count()).select_from(Review)) == 0


def test_cooldown_limits_repeated_syncs(client, session, tenants, yelp, monkeypatch):
    monkeypatch.setattr(settings(), "sync_cooldown_seconds", 300)
    a = tenants[0]
    connection_id = connected(client, a)["id"]
    work(session, yelp)
    response = sync(client, a, connection_id)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "sync_cooldown"
    assert 0 < int(response.headers["retry-after"]) <= 300


def test_cross_tenant_isolation(client, session, tenants, yelp):
    a, b = tenants
    connection = connected(client, a)
    work(session, yelp)
    account_id = client.get(f"/v1/locations/{connection['location_id']}",
                            headers=headers(a)).json()["account_id"]
    job_id = latest_job(client, a, connection["id"])["id"]
    for path in [f"/v1/accounts/{account_id}/reviews", f"/v1/sync-jobs/{job_id}",
                 f"/v1/connections/{connection['id']}/sync-jobs"]:
        assert client.get(path, headers=headers(b)).status_code == 404
    assert sync(client, b, connection["id"]).status_code == 404
    assert feed(client, b)["meta"]["total"] == 0


def test_feeds_scope_by_account_and_hide_disabled(client, session, tenants, yelp):
    a = tenants[0]
    first = connected(client, a, n=1)
    connected(client, a, n=2)
    work(session, yelp)
    account_id = client.get(f"/v1/locations/{first['location_id']}",
                            headers=headers(a)).json()["account_id"]
    account_feed = f"/v1/accounts/{account_id}/reviews"
    assert feed(client, a)["meta"]["total"] == 6
    body = feed(client, a, account_feed)
    assert body["meta"]["total"] == 3
    assert {row["account_id"] for row in body["data"]} == {account_id}

    client.post(f"/v1/connections/{first['id']}/disable", headers=headers(a),
                json={"reason": "incorrect_business"})
    assert feed(client, a)["meta"]["total"] == 3
    assert feed(client, a, account_feed)["meta"]["total"] == 0


def test_feed_pagination(client, session, tenants, yelp):
    a = tenants[0]
    connected(client, a)
    work(session, yelp)
    first = feed(client, a, "/v1/partner/reviews?limit=2&page=1")
    second = feed(client, a, "/v1/partner/reviews?limit=2&page=2")
    assert first["meta"] == {"page": 1, "limit": 2, "total": 3, "last_page": 2}
    assert len(first["data"]) == 2 and len(second["data"]) == 1
    dates = [row["reviewed_at"] for row in first["data"] + second["data"]]
    assert dates == sorted(dates, reverse=True)
    for query in ["limit=250", "limit=0", "page=0"]:
        response = client.get(f"/v1/partner/reviews?{query}", headers=headers(a))
        assert response.status_code == 422


def test_database_rejects_cross_tenant_review(client, session, tenants, yelp):
    a, b = tenants
    connection_id = connected(client, a)["id"]
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(Review(partner_id=b[0].id, connection_id=connection_id,
                           external_review_id="x", rating=5, text="x", author_name="x",
                           review_url="https://yelp.invalid", reviewed_at=now,
                           content_hash="x", content_updated_at=now, last_seen_at=now))
        session.flush()
