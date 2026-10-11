from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models import Account, Location, ReviewSiteConnection
from app.yelp import SyntheticYelp, yelp_client


def headers(tenant):
    return {"Authorization": f"Bearer {tenant[2]}"}


def business(n, prefix="rbtest"):
    # Invented IDs in Yelp's 22-character format; never a real business.
    return f"{prefix}{n:0{22 - len(prefix)}d}"


def new_location(client, tenant, name="Georgetown"):
    account_id = client.post(f"/v1/partners/{tenant[0].id}/accounts", headers=headers(tenant),
                             json={"name": "Brightwell Dental"}).json()["id"]
    return client.post(f"/v1/accounts/{account_id}/locations", headers=headers(tenant),
                       json={"name": name}).json()["id"]


def connect(client, tenant, location_id, n=1, business_id=None):
    body = {"provider": "yelp", "external_business_id": business_id or business(n)}
    return client.post(f"/v1/locations/{location_id}/connections", headers=headers(tenant),
                       json=body)


def disable(client, tenant, connection_id, reason="incorrect_business"):
    return client.post(f"/v1/connections/{connection_id}/disable", headers=headers(tenant),
                       json={"reason": reason})


def connection_count(session):
    return session.scalar(select(func.count()).select_from(ReviewSiteConnection))


class RecordingYelp(SyntheticYelp):
    def __init__(self):
        self.calls = []

    def lookup(self, business_id):
        self.calls.append(business_id)
        return super().lookup(business_id)


@pytest.fixture
def yelp(client):
    fake = RecordingYelp()
    app.dependency_overrides[yelp_client] = lambda: fake
    return fake


def test_review_site_capabilities(client, tenants):
    assert client.get("/v1/review-sites").status_code == 401
    response = client.get("/v1/review-sites", headers=headers(tenants[0]))
    assert response.status_code == 200
    assert response.json() == [{
        "provider": "yelp", "reviews": "sample", "max_reviews_per_fetch": 3,
        "excerpt_max_chars": 160,
        "full_history": False, "deletion_detection": False, "reply_posting": False,
        "content_max_age_hours": 24,
    }]


def test_connection_lifecycle_and_cross_tenant_isolation(client, tenants):
    a, b = tenants
    location_id = new_location(client, a)
    response = connect(client, a, location_id)
    assert response.status_code == 201
    created = response.json()
    assert created["partner_id"] == str(a[0].id)
    assert created["location_id"] == location_id
    assert created["external_business_id"] == business(1)
    assert created["verified_at"] is not None
    assert created["disabled_at"] is None and created["disabled_reason"] is None
    assert created["business"] == {"name": "Synthetic Business 0001",
                                   "address": "0001 Example Street, Testville",
                                   "url": f"https://yelp.invalid/biz/{business(1)}"}
    connection_id = created["id"]

    # Provider details are shown once and never stored.
    fetched = client.get(f"/v1/connections/{connection_id}", headers=headers(a)).json()
    assert "business" not in fetched
    assert fetched == {key: value for key, value in created.items() if key != "business"}

    for path in [f"/v1/connections/{connection_id}", f"/v1/locations/{location_id}/connections"]:
        assert client.get(path, headers=headers(a)).status_code == 200
        assert client.get(path, headers=headers(b)).status_code == 404
    assert connect(client, b, location_id, n=2).status_code == 404
    assert disable(client, b, connection_id).status_code == 404

    first = disable(client, a, connection_id)
    assert first.status_code == 200
    assert first.json()["disabled_at"] is not None
    assert first.json()["disabled_reason"] == "incorrect_business"
    # Repeating keeps the original time and reason.
    second = disable(client, a, connection_id, reason="no_longer_needed")
    assert second.json()["disabled_at"] == first.json()["disabled_at"]
    assert second.json()["disabled_reason"] == "incorrect_business"


def test_unknown_business_is_rejected_and_not_stored(client, session, tenants):
    a = tenants[0]
    response = connect(client, a, new_location(client, a), business_id=business(1, "rbnone"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "business_not_found"
    assert connection_count(session) == 0


def test_provider_outage_is_retryable_and_not_stored(client, session, tenants):
    a = tenants[0]
    location_id = new_location(client, a)
    response = connect(client, a, location_id, business_id=business(1, "rbdown"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert response.headers["retry-after"] == "30"
    assert connection_count(session) == 0
    assert connect(client, a, location_id).status_code == 201


def test_provider_not_called_for_requests_that_would_fail(client, tenants, yelp):
    a, b = tenants
    location_id = new_location(client, a)
    other = new_location(client, a, "Capitol Hill")
    assert connect(client, a, location_id, n=1).status_code == 201
    assert yelp.calls == [business(1)]
    assert connect(client, b, location_id, n=2).status_code == 404
    assert connect(client, a, location_id, n=3).status_code == 409
    assert connect(client, a, other, n=1).status_code == 409
    assert connect(client, a, uuid4(), n=4).status_code == 404
    assert yelp.calls == [business(1)]


def test_one_active_connection_per_location(client, tenants):
    a = tenants[0]
    location_id = new_location(client, a)
    first_id = connect(client, a, location_id, n=1).json()["id"]
    response = connect(client, a, location_id, n=2)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "connection_exists"

    # Correcting the business means disabling and connecting again.
    disable(client, a, first_id)
    second_id = connect(client, a, location_id, n=2).json()["id"]
    path = f"/v1/locations/{location_id}/connections"
    active = client.get(path, headers=headers(a)).json()
    everything = client.get(f"{path}?include_disabled=true", headers=headers(a)).json()
    assert [row["id"] for row in active] == [second_id]
    assert {row["id"] for row in everything} == {first_id, second_id}


def test_one_location_per_business_within_a_partner(client, tenants):
    a, b = tenants
    first = new_location(client, a, "Georgetown")
    second = new_location(client, a, "Capitol Hill")
    first_id = connect(client, a, first).json()["id"]
    response = connect(client, a, second)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "business_already_connected"
    # Other partners are unaffected, so the conflict reveals nothing across tenants.
    assert connect(client, b, new_location(client, b)).status_code == 201
    disable(client, a, first_id, reason="no_longer_needed")
    assert connect(client, a, second).status_code == 201


@pytest.mark.parametrize("body", [
    {"provider": "google", "external_business_id": business(1)},
    {"provider": "yelp", "external_business_id": "too-short"},
    {"provider": "yelp", "external_business_id": "gary-danko-san-francisco"},
    {"provider": "yelp", "external_business_id": "has spaces in it 000000"},
    {"provider": "yelp"},
    {"provider": "yelp", "external_business_id": business(1), "partner_id": "override"},
])
def test_connection_validation(client, tenants, body):
    a = tenants[0]
    response = client.post(f"/v1/locations/{new_location(client, a)}/connections",
                           headers=headers(a), json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("body", [None, {}, {"reason": "mistake"},
                                  {"reason": "incorrect_business", "disabled_at": "2020"}])
def test_disable_requires_a_valid_reason(client, tenants, body):
    a = tenants[0]
    connection_id = connect(client, a, new_location(client, a)).json()["id"]
    response = client.post(f"/v1/connections/{connection_id}/disable", headers=headers(a),
                           json=body)
    assert response.status_code == 422
    fetched = client.get(f"/v1/connections/{connection_id}", headers=headers(a)).json()
    assert fetched["disabled_at"] is None


def test_business_id_is_immutable(client, tenants):
    a = tenants[0]
    connection_id = connect(client, a, new_location(client, a)).json()["id"]
    for method in ["PATCH", "PUT"]:
        response = client.request(method, f"/v1/connections/{connection_id}", headers=headers(a),
                                  json={"external_business_id": business(2)})
        assert response.status_code == 405


def test_connection_pagination_and_unknown(client, tenants):
    a = tenants[0]
    location_id = new_location(client, a)
    ids = []
    for n in range(3):
        ids.append(connect(client, a, location_id, n=n).json()["id"])
        disable(client, a, ids[-1])
    path = f"/v1/locations/{location_id}/connections?include_disabled=true"
    first = client.get(f"{path}&limit=2&page=1", headers=headers(a)).json()
    second = client.get(f"{path}&limit=2&page=2", headers=headers(a)).json()
    assert len(first) == 2 and len(second) == 1
    assert {row["id"] for row in first + second} == set(ids)
    for query in ["limit=250", "limit=0", "page=0"]:
        assert client.get(f"{path}&{query}", headers=headers(a)).status_code == 422
    for path in [f"/v1/connections/{uuid4()}", f"/v1/locations/{uuid4()}/connections"]:
        assert client.get(path, headers=headers(a)).status_code == 404


@pytest.fixture
def private_location(session, tenants):
    owner = tenants[0][0]
    account = Account(partner_id=owner.id, name="Private")
    session.add(account)
    session.flush()
    location = Location(partner_id=owner.id, account_id=account.id, name="Private")
    session.add(location)
    session.flush()
    return location


@pytest.mark.parametrize("overrides", [
    {"partner": "other"},
    {"provider": "google"},
    {"disabled_at": datetime.now(UTC)},
    {"disabled_reason": "no_longer_needed"},
    {"disabled_at": datetime.now(UTC), "disabled_reason": "mistake"},
])
def test_database_rejects_invalid_connections(session, tenants, private_location, overrides):
    overrides = dict(overrides)
    partner = tenants[1 if overrides.pop("partner", None) else 0][0]
    values = {"provider": "yelp", "external_business_id": business(1),
              "verified_at": datetime.now(UTC), **overrides}
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ReviewSiteConnection(partner_id=partner.id, location_id=private_location.id,
                                         **values))
        session.flush()
