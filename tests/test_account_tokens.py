from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from test_connections import connect, headers
from test_reviews import FakeYelp, latest_job, no_cooldown, work  # noqa: F401

from app.auth import hash_token
from app.models import ApiToken


def bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def new_account(client, tenant, name="Brightwell Dental"):
    return client.post(f"/v1/partners/{tenant[0].id}/accounts", headers=headers(tenant),
                       json={"name": name}).json()["id"]


def new_location(client, tenant, account_id):
    return client.post(f"/v1/accounts/{account_id}/locations", headers=headers(tenant),
                       json={"name": "Georgetown"}).json()["id"]


def issue(client, tenant, account_id, label="customer portal", **body):
    return client.post(f"/v1/accounts/{account_id}/tokens", headers=headers(tenant),
                       json={"label": label, **body})


@pytest.fixture
def estate(client, session, tenants):
    """One partner with two accounts, each with a synced connection, and a token for the
    first account."""
    a = tenants[0]
    ids = {}
    for key, n in [("x", 1), ("y", 2)]:
        account_id = new_account(client, a)
        location_id = new_location(client, a, account_id)
        connection_id = connect(client, a, location_id, n=n).json()["id"]
        ids[key] = {"account": account_id, "location": location_id,
                    "connection": connection_id}
    work(session, FakeYelp())
    for key in ids:
        ids[key]["job"] = latest_job(client, a, ids[key]["connection"])["id"]
    response = issue(client, a, ids["x"]["account"])
    assert response.status_code == 201
    return {**ids, "partner": a, "secret": response.json()["token"],
            "token_id": response.json()["id"]}


def test_issue_and_identify(client, session, estate):
    a, x = estate["partner"], estate["x"]
    secret = estate["secret"]
    assert secret.startswith("rb_")
    me = client.get("/v1/token", headers=bearer(secret)).json()
    assert (me["account_id"], me["partner_id"], me["label"]) == (
        x["account"], str(a[0].id), "customer portal")
    assert me["prefix"] == secret[:12] and "token" not in me
    assert client.get("/v1/token", headers=headers(a)).json()["account_id"] is None
    stored = session.get(ApiToken, me["id"])
    assert stored.token_hash == hash_token(secret)
    listed = client.get(f"/v1/accounts/{x['account']}/tokens", headers=headers(a)).json()
    assert [row["id"] for row in listed] == [me["id"]]
    assert secret not in str(listed)


def test_account_token_works_within_its_account(client, estate):
    x, auth = estate["x"], bearer(estate["secret"])
    for path in [f"/v1/accounts/{x['account']}", f"/v1/accounts/{x['account']}/locations",
                 f"/v1/locations/{x['location']}",
                 f"/v1/locations/{x['location']}/connections",
                 f"/v1/connections/{x['connection']}",
                 f"/v1/connections/{x['connection']}/sync-jobs", f"/v1/sync-jobs/{x['job']}",
                 f"/v1/accounts/{x['account']}/reviews",
                 f"/v1/accounts/{x['account']}/reviews?location_id={x['location']}"]:
        assert client.get(path, headers=auth).status_code == 200, path
    assert client.get(f"/v1/accounts/{x['account']}/reviews",
                      headers=auth).json()["meta"]["total"] == 3
    location_id = client.post(f"/v1/accounts/{x['account']}/locations", headers=auth,
                              json={"name": "Dupont"}).json()["id"]
    created = client.post(f"/v1/locations/{location_id}/connections", headers=auth,
                          json={"provider": "yelp",
                                "external_business_id": "rbtest0000000000000003"})
    assert created.status_code == 201
    assert client.post(f"/v1/connections/{x['connection']}/sync",
                       headers=auth).status_code == 202
    assert client.post(f"/v1/connections/{created.json()['id']}/disable", headers=auth,
                       json={"reason": "no_longer_needed"}).status_code == 200


def test_other_accounts_are_unknown(client, estate):
    x, y, auth = estate["x"], estate["y"], bearer(estate["secret"])
    for path in [f"/v1/accounts/{y['account']}", f"/v1/accounts/{y['account']}/locations",
                 f"/v1/locations/{y['location']}",
                 f"/v1/locations/{y['location']}/connections",
                 f"/v1/connections/{y['connection']}",
                 f"/v1/connections/{y['connection']}/sync-jobs", f"/v1/sync-jobs/{y['job']}",
                 f"/v1/accounts/{y['account']}/reviews",
                 f"/v1/accounts/{x['account']}/reviews?location_id={y['location']}"]:
        assert client.get(path, headers=auth).status_code == 404, path
    for path, body in [
        (f"/v1/accounts/{y['account']}/locations", {"name": "Intrusion"}),
        (f"/v1/locations/{y['location']}/connections",
         {"provider": "yelp", "external_business_id": "rbtest0000000000000004"}),
        (f"/v1/connections/{y['connection']}/sync", None),
        (f"/v1/connections/{y['connection']}/disable", {"reason": "no_longer_needed"}),
    ]:
        assert client.post(path, headers=auth, json=body).status_code == 404, path


def test_partner_endpoints_refuse_account_tokens(client, estate):
    a, x, auth = estate["partner"], estate["x"], bearer(estate["secret"])
    requests = [
        ("get", "/v1/partner", None),
        ("get", "/v1/partner/accounts", None),
        ("post", f"/v1/partners/{a[0].id}/accounts", {"name": "Escalation"}),
        ("get", "/v1/partner/reviews", None),
        ("post", f"/v1/accounts/{x['account']}/tokens", {"label": "self-issued"}),
        ("get", f"/v1/accounts/{x['account']}/tokens", None),
        ("post", f"/v1/tokens/{estate['token_id']}/revoke", None),
    ]
    for method, path, body in requests:
        response = getattr(client, method)(path, headers=auth,
                                           **({"json": body} if body else {}))
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "insufficient_scope"


def test_other_partners_cannot_issue_or_revoke(client, estate, tenants):
    b, x = tenants[1], estate["x"]
    assert issue(client, b, x["account"]).status_code == 404
    assert client.get(f"/v1/accounts/{x['account']}/tokens",
                      headers=headers(b)).status_code == 404
    assert client.post(f"/v1/tokens/{estate['token_id']}/revoke",
                       headers=headers(b)).status_code == 404
    assert client.get("/v1/token", headers=bearer(estate["secret"])).status_code == 200


def test_revocation(client, estate):
    a, secret = estate["partner"], estate["secret"]
    first = client.post(f"/v1/tokens/{estate['token_id']}/revoke", headers=headers(a))
    assert first.status_code == 200 and first.json()["revoked_at"]
    again = client.post(f"/v1/tokens/{estate['token_id']}/revoke", headers=headers(a))
    assert again.json()["revoked_at"] == first.json()["revoked_at"]
    assert client.get("/v1/token", headers=bearer(secret)).status_code == 401
    # Partner tokens are revoked by the operator, not through the API.
    assert client.post(f"/v1/tokens/{a[1].id}/revoke", headers=headers(a)).status_code == 404


def test_active_token_cap(client, estate):
    a, x = estate["partner"], estate["x"]
    issued = [issue(client, a, x["account"], label=f"t{n}") for n in range(9)]
    assert all(response.status_code == 201 for response in issued)
    over = issue(client, a, x["account"])
    assert (over.status_code, over.json()["error"]["code"]) == (409, "token_limit")
    client.post(f"/v1/tokens/{issued[0].json()['id']}/revoke", headers=headers(a))
    assert issue(client, a, x["account"]).status_code == 201


@pytest.mark.parametrize("body", [{"label": ""}, {"label": "x", "expires_in_days": 0},
                                  {"label": "x", "expires_in_days": 366},
                                  {"label": "x", "account_id": "override"}])
def test_issue_validation(client, estate, body):
    response = client.post(f"/v1/accounts/{estate['x']['account']}/tokens",
                           headers=headers(estate["partner"]), json=body)
    assert response.status_code == 422


def test_expiry(client, estate):
    a, x = estate["partner"], estate["x"]
    expires = datetime.fromisoformat(
        issue(client, a, x["account"], expires_in_days=7).json()["expires_at"])
    assert abs(expires - (datetime.now(UTC) + timedelta(days=7))) < timedelta(minutes=1)


def test_database_keeps_account_tokens_inside_their_partner(session, estate, tenants):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ApiToken(partner_id=tenants[1][0].id, account_id=estate["x"]["account"],
                             token_hash="0" * 64, prefix="rb_x", label="forged",
                             expires_at=datetime.now(UTC) + timedelta(days=1)))
        session.flush()
