from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app import services
from app.auth import Scope, hash_token
from app.errors import ServiceError
from app.models import Account, Location


def headers(tenant):
    return {"Authorization": f"Bearer {tenant[2]}"}


def test_onboarding_and_cross_tenant_isolation(client, tenants):
    a, b = tenants
    response = client.post(f"/v1/partners/{a[0].id}/accounts", headers=headers(a),
                           json={"name": "Brightwell Dental"})
    assert response.status_code == 201
    account_id = response.json()["id"]
    response = client.post(f"/v1/accounts/{account_id}/locations", headers=headers(a),
                           json={"name": "Georgetown"})
    assert response.status_code == 201
    location_id = response.json()["id"]
    for path in [f"/v1/accounts/{account_id}", f"/v1/accounts/{account_id}/locations",
                 f"/v1/locations/{location_id}"]:
        assert client.get(path, headers=headers(a)).status_code == 200
        assert client.get(path, headers=headers(b)).status_code == 404
    assert client.post(f"/v1/accounts/{account_id}/locations", headers=headers(b),
                       json={"name": "Intrusion"}).status_code == 404
    assert client.post(f"/v1/partners/{a[0].id}/accounts", headers=headers(b),
                       json={"name": "Intrusion"}).status_code == 404
    assert client.get("/v1/partner/accounts", headers=headers(b)).json() == []
    assert len(client.get("/v1/partner/accounts", headers=headers(a)).json()) == 1


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic abc", "Bearer"])
def test_invalid_authentication(client, authorization):
    response = client.get("/v1/partner", headers=(
        {"Authorization": authorization} if authorization else {}
    ))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_token_lifecycle(client, session, tenants, state):
    tenant = tenants[0]
    token = tenant[1]
    if state == "expired":
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        token.revoked_at = datetime.now(UTC)
    session.commit()
    assert client.get("/v1/partner", headers=headers(tenant)).status_code == 401


def test_hash_only_storage(tenants):
    _, token, secret = tenants[0]
    assert token.token_hash == hash_token(secret)
    assert token.token_hash != secret
    assert len(token.token_hash) == 64


def test_service_layer_enforces_scope(session, tenants):
    a, b = tenants
    account = Account(partner_id=a[0].id, name="Private")
    session.add(account)
    session.flush()
    scope = Scope(partner_id=b[0].id, token_id=b[1].id)
    with pytest.raises(ServiceError) as error:
        services.create_location(session, scope, account.id, "Forbidden")
    assert error.value.status == 404


def test_database_rejects_cross_tenant_parent(session, tenants):
    a, b = tenants
    account = Account(partner_id=a[0].id, name="Private")
    session.add(account)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(Location(partner_id=b[0].id, account_id=account.id, name="Invalid"))
        session.flush()


@pytest.mark.parametrize("body", [{"name": "   "}, {"name": "x", "partner_id": "override"}])
def test_validation(client, tenants, body):
    a = tenants[0]
    response = client.post(f"/v1/partners/{a[0].id}/accounts", headers=headers(a), json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_pagination_and_unknown_resources(client, tenants):
    a = tenants[0]
    for name in ["First", "Second", "Third"]:
        assert client.post(f"/v1/partners/{a[0].id}/accounts", headers=headers(a),
                           json={"name": name}).status_code == 201
    first = client.get("/v1/partner/accounts?limit=2&page=1", headers=headers(a)).json()
    second = client.get("/v1/partner/accounts?limit=2&page=2", headers=headers(a)).json()
    assert len(first) == 2 and len(second) == 1
    assert len({row["id"] for row in first + second}) == 3
    for query in ["limit=250", "limit=0", "page=0"]:
        assert client.get(f"/v1/partner/accounts?{query}", headers=headers(a)).status_code == 422
    assert client.get(f"/v1/accounts/{uuid4()}", headers=headers(a)).status_code == 404


def test_location_validation_pagination_and_unknown(client, tenants):
    a = tenants[0]
    account_id = client.post(f"/v1/partners/{a[0].id}/accounts", headers=headers(a),
                             json={"name": "Brightwell Dental"}).json()["id"]
    path = f"/v1/accounts/{account_id}/locations"
    for body in [{"name": "   "}, {"name": "x", "account_id": "override"}]:
        response = client.post(path, headers=headers(a), json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    for name in ["First", "Second", "Third"]:
        assert client.post(path, headers=headers(a), json={"name": name}).status_code == 201
    first = client.get(f"{path}?limit=2&page=1", headers=headers(a)).json()
    second = client.get(f"{path}?limit=2&page=2", headers=headers(a)).json()
    assert len(first) == 2 and len(second) == 1
    assert len({row["id"] for row in first + second}) == 3
    for query in ["limit=250", "limit=0", "page=0"]:
        assert client.get(f"{path}?{query}", headers=headers(a)).status_code == 422
    response = client.get(f"/v1/locations/{uuid4()}", headers=headers(a))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_partner(client, tenants):
    a, b = tenants
    for partner, _, secret in [a, b]:
        response = client.get("/v1/partner", headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(partner.id)
        assert body["name"] == partner.name


def test_health_and_openapi(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/v1/partner"]["get"]["security"] == [{"HTTPBearer": []}]
