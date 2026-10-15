import base64
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from test_connections import business, connect, disable, headers, new_location
from test_reviews import FakeYelp, connected, no_cooldown, stored, sync, work  # noqa: F401

from app import jobs
from app.main import app
from app.services import encode_cursor
from app.yelp import SyntheticYelp, yelp_client


@pytest.fixture
def yelp(client):
    fake = FakeYelp()
    app.dependency_overrides[yelp_client] = lambda: fake
    return fake


def get(client, tenant, path="/v1/partner/reviews", **params):
    return client.get(f"{path}?{urlencode(params, doseq=True)}", headers=headers(tenant))


def at(row, field):
    return datetime.fromisoformat(row[field])


def ids(response):
    return [row["id"] for row in response.json()["data"]]


def sync_all(client, tenant, limit, **params):
    """Pages through sort=updated_at by cursor, as a client would; returns pages of IDs."""
    pages, cursor = [], None
    for _ in range(50):  # A broken cursor must fail, not loop forever.
        extra = {"cursor": cursor} if cursor else {}
        body = get(client, tenant, sort="updated_at", limit=limit, **params, **extra).json()
        pages.append([row["id"] for row in body["data"]])
        cursor = body["meta"]["next_cursor"]
        if cursor is None:
            return pages
    pytest.fail("cursor paging never ended")


def test_filters(client, session, tenants, yelp):
    a, b = tenants
    first = connected(client, a, n=1)
    second = connected(client, a, n=2)
    work(session, yelp)
    # The synthetic reviews: ratings 5, 4, 2 dated 2026-03-02, 2026-02-11, 2026-01-20.
    totals = {
        (): 6,
        (("rating", (5, 4)),): 4,
        (("rating", 2),): 2,
        (("provider", "yelp"),): 6,
        (("location_id", first["location_id"]),): 3,
        (("reviewed_from", "2026-02-01T00:00:00Z"),): 4,
        (("reviewed_to", "2026-02-01T00:00:00Z"),): 2,
        (("reviewed_from", "2026-02-01T00:00:00Z"), ("reviewed_to", "2026-03-01T00:00:00Z")): 2,
        (("created_since", (jobs.utcnow() + timedelta(hours=1)).isoformat()),): 0,
        (("updated_since", (jobs.utcnow() - timedelta(hours=1)).isoformat()),): 6,
    }
    for params, total in totals.items():
        response = get(client, a, **dict(params))
        assert response.status_code == 200, params
        assert response.json()["meta"]["total"] == total, params
    rows = get(client, a, location_id=second["location_id"]).json()["data"]
    assert {row["connection_id"] for row in rows} == {second["id"]}

    # Locations outside the partner, or outside the account being read, are unknown.
    account_id = client.get(f"/v1/locations/{first['location_id']}",
                            headers=headers(a)).json()["account_id"]
    other_account_location = second["location_id"]
    assert get(client, a, f"/v1/accounts/{account_id}/reviews",
               location_id=other_account_location).status_code == 404
    assert get(client, b, location_id=first["location_id"]).status_code == 404


@pytest.mark.parametrize("params", [
    {"sort": "updated_at", "page": 1},
    {"cursor": "abc"},
    {"reviewed_from": "2026-03-01T00:00:00Z", "reviewed_to": "2026-02-01T00:00:00Z"},
    {"updated_since": "2026-01-01T00:00:00"},
    {"updatedSince": "2026-01-01T00:00:00Z"},
    {"rating": 6},
    {"sort": "rating"},
    {"page": 401, "limit": 25},
    {"limit": 101},
])
def test_invalid_queries(client, tenants, params):
    response = get(client, tenants[0], **params)
    assert response.status_code == 422, params
    assert response.json()["error"]["code"] == "validation_error"


def test_deepest_allowed_page(client, tenants):
    assert get(client, tenants[0], page=400, limit=25).status_code == 200


NAIVE = base64.urlsafe_b64encode(json.dumps(["2026-01-01T00:00:00", str(uuid4())]).encode())


@pytest.mark.parametrize("cursor", ["!!!", "bm90IGpzb24", NAIVE.decode(), encode_cursor(
    datetime.fromisoformat("2026-01-01T00:00:00+00:00"), uuid4())[:-3]])
def test_malformed_cursor(client, tenants, cursor):
    response = get(client, tenants[0], sort="updated_at", cursor=cursor)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_cursor_sync_skips_nothing_when_rows_change_mid_sync(client, session, tenants, yelp):
    """The case page numbers get wrong: a row changes after the client read page 1, so it
    moves to the end. With offsets, the rows behind it shift back and one is never read."""
    a = tenants[0]
    start = jobs.utcnow()
    connections = []
    for n in range(1, 4):
        connections.append(connected(client, a, n=n))
        work(session, yelp, start + timedelta(seconds=n))
    every = set(ids(get(client, a, limit=100)))
    assert len(every) == 9

    body = get(client, a, sort="updated_at", limit=2).json()
    seen = [row["id"] for row in body["data"]]
    assert "total" not in body["meta"] and body["meta"]["next_cursor"]

    # Edit one of the second connection's reviews, which the client has not reached yet.
    edited = SyntheticYelp().reviews(business(2))
    edited[0]["text"] = "Edited after the client started syncing."
    yelp.raw = edited
    assert sync(client, a, connections[1]["id"]).status_code == 202
    work(session, yelp, start + timedelta(seconds=10))
    edited_id = str(stored(session, edited[0]["id"]).id)

    cursor = body["meta"]["next_cursor"]
    for _ in range(50):
        if cursor is None:
            break
        body = get(client, a, sort="updated_at", limit=2, cursor=cursor).json()
        seen += [row["id"] for row in body["data"]]
        cursor = body["meta"]["next_cursor"]
    assert set(seen) == every
    assert seen[-1] == edited_id and len(seen) == len(every)


def test_cursor_pages_partition_the_feed(client, session, tenants, yelp):
    a = tenants[0]
    connected(client, a, n=1)
    connected(client, a, n=2)
    work(session, yelp)  # One timestamp for all six rows: ties are broken by ID.
    pages = sync_all(client, a, limit=4)
    assert [len(page) for page in pages] == [4, 2]
    flat = [row for page in pages for row in page]
    assert len(set(flat)) == 6 and set(flat) == set(ids(get(client, a)))
    assert sync_all(client, a, limit=6) == [flat]


def test_removals_reach_incremental_sync(client, session, tenants, yelp):
    a = tenants[0]
    connection = connected(client, a)
    work(session, yelp)
    before_disable = jobs.utcnow()
    assert disable(client, a, connection["id"], "incorrect_business").status_code == 200

    assert get(client, a).json()["meta"]["total"] == 0
    changed = dict(sort="updated_at", updated_since=before_disable.isoformat())
    assert get(client, a, **changed).json()["data"] == []
    rows = get(client, a, include_removed="true", **changed).json()["data"]
    assert len(rows) == 3
    for row in rows:
        assert row["removed_reason"] == "incorrect_business"
        assert row["removed_at"] == row["updated_at"]
        assert at(row, "updated_at") > at(row, "content_updated_at")

    # Active reviews report no removal.
    active = connected(client, a, n=2)
    work(session, yelp)
    rows = get(client, a, include_removed="true", location_id=active["location_id"]).json()
    assert {(row["removed_at"], row["removed_reason"]) for row in rows["data"]} == {
        (None, None)}


def test_removal_never_moves_updated_at_backwards(client, session, tenants, yelp):
    """A worker whose clock runs ahead writes a later updated_at than the disable's."""
    a = tenants[0]
    connection = connected(client, a)
    ahead = jobs.utcnow() + timedelta(hours=1)
    work(session, yelp, ahead)
    disable(client, a, connection["id"], "no_longer_needed")
    session.expire_all()
    rows = get(client, a, include_removed="true").json()["data"]
    assert len(rows) == 3 and all(at(row, "updated_at") == ahead for row in rows)


def test_browsing_is_unchanged(client, session, tenants, yelp):
    a = tenants[0]
    location_id = new_location(client, a)
    connect(client, a, location_id, n=1)
    work(session, yelp)
    body = get(client, a, limit=2).json()
    assert body["meta"] == {"page": 1, "limit": 2, "total": 3, "last_page": 2}
    dates = [row["reviewed_at"] for row in body["data"]]
    assert dates == sorted(dates, reverse=True)
