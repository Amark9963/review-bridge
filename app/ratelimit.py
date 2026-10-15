"""Inbound rate limits: requests per token, per endpoint group, per one-minute window.

Counters live in PostgreSQL because V1 has no Redis. That costs one small upsert per
authenticated request; at this project's scale that is cheap, and it stays correct across
several API processes. Fixed windows allow a burst of up to twice the limit across a window
boundary, an accepted simplification.

Only authenticated requests are counted: an unknown token has nothing to count against,
and guessing 256-bit tokens is infeasible. Limiting by IP belongs at a proxy in front.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import ServiceError
from app.models import RateLimitCounter

WINDOW = timedelta(minutes=1)
# Route templates outside the default group; each group has its own counter.
GROUPS = {
    "/v1/partner/reviews": "feed",
    "/v1/accounts/{account_id}/reviews": "feed",
    "/v1/accounts/{account_id}/tokens": "tokens",
    "/v1/tokens/{token_id}/revoke": "tokens",
}


@dataclass(frozen=True)
class Usage:
    limit: int
    remaining: int
    reset_at: datetime

    def headers(self) -> dict[str, str]:
        return {"X-RateLimit-Limit": str(self.limit),
                "X-RateLimit-Remaining": str(self.remaining)}


def limit_for(group: str) -> int:
    return {"feed": settings().rate_limit_feed_per_minute,
            "tokens": settings().rate_limit_tokens_per_minute,
            }.get(group, settings().rate_limit_default_per_minute)


def hit(session: Session, token_id, route: str | None, now: datetime) -> Usage:
    """Counts one request and commits, independent of the request's own work. Raises 429
    once the token has used its group's allowance for this window."""
    group = GROUPS.get(route, "default")
    window_start = now.replace(second=0, microsecond=0)
    count = session.scalar(insert(RateLimitCounter)
        .values(token_id=token_id, bucket=group, window_start=window_start, count=1)
        .on_conflict_do_update(index_elements=["token_id", "bucket", "window_start"],
                               set_={"count": RateLimitCounter.count + 1})
        .returning(RateLimitCounter.count))
    session.commit()
    limit = limit_for(group)
    usage = Usage(limit, max(0, limit - count), window_start + WINDOW)
    if count > limit:
        retry_after = max(1, int((usage.reset_at - now).total_seconds() + 0.999))
        raise ServiceError(429, "rate_limited", "Too many requests; retry later", headers={
            **usage.headers(), "Retry-After": str(retry_after),
            "X-RateLimit-Reset": str(int(usage.reset_at.timestamp()))})
    return usage


def prune(session: Session, now: datetime) -> int:
    """Deletes finished windows; run periodically by the worker."""
    deleted = session.execute(delete(RateLimitCounter)
        .where(RateLimitCounter.window_start < now - timedelta(hours=1))).rowcount
    session.commit()
    return deleted
