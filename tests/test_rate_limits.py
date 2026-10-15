from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_connections import headers

from app import ratelimit
from app.config import settings
from app.errors import ServiceError
from app.models import RateLimitCounter


@pytest.fixture
def limits(monkeypatch):
    for name, value in [("rate_limit_default_per_minute", 3),
                        ("rate_limit_feed_per_minute", 2),
                        ("rate_limit_tokens_per_minute", 1)]:
        monkeypatch.setattr(settings(), name, value)


def test_headers_count_down_then_429(client, tenants, limits):
    a = tenants[0]
    remaining = [client.get("/v1/partner", headers=headers(a)).headers["x-ratelimit-remaining"]
                 for _ in range(3)]
    assert remaining == ["2", "1", "0"]
    response = client.get("/v1/partner", headers=headers(a))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["x-ratelimit-limit"] == "3"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert int(response.headers["x-ratelimit-reset"]) > datetime.now(UTC).timestamp() - 1


def test_groups_and_tokens_count_separately(client, tenants, limits):
    a, b = tenants
    for _ in range(3):
        client.get("/v1/partner", headers=headers(a))
    assert client.get("/v1/partner", headers=headers(a)).status_code == 429
    # The feed has its own allowance, and another token its own counters.
    feed = client.get("/v1/partner/reviews", headers=headers(a))
    assert (feed.status_code, feed.headers["x-ratelimit-limit"]) == (200, "2")
    assert client.get("/v1/partner", headers=headers(b)).status_code == 200


def test_failed_requests_count_and_anonymous_ones_are_not_limited(client, tenants, limits):
    a = tenants[0]
    for _ in range(3):
        assert client.get("/v1/accounts/not-a-uuid", headers=headers(a)).status_code == 422
    assert client.get("/v1/partner", headers=headers(a)).status_code == 429
    anonymous = client.get("/v1/partner")
    assert anonymous.status_code == 401 and "x-ratelimit-limit" not in anonymous.headers
    assert "x-ratelimit-limit" not in client.get("/health/live").headers


def test_window_resets_and_old_windows_are_pruned(session, tenants, limits):
    token_id = tenants[0][1].id
    minute = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    for second in range(3):
        ratelimit.hit(session, token_id, None, minute + timedelta(seconds=second))
    with pytest.raises(ServiceError) as error:
        ratelimit.hit(session, token_id, None, minute + timedelta(seconds=50))
    assert error.value.headers["Retry-After"] == "10"
    usage = ratelimit.hit(session, token_id, None, minute + timedelta(minutes=1))
    assert usage.remaining == 2
    assert ratelimit.prune(session, minute + timedelta(minutes=61)) == 1
    assert session.scalar(select(func.count()).select_from(RateLimitCounter)) == 1
