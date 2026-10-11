import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ServiceError
from app.models import ApiToken


@dataclass(frozen=True)
class Scope:
    partner_id: UUID
    token_id: UUID


def hash_token(secret: str) -> str:
    # SHA-256 is appropriate for high-entropy machine tokens, not human passwords.
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_token(session: Session, partner_id: UUID, label: str, days: int = 90):
    secret = "rb_" + secrets.token_urlsafe(32)
    token = ApiToken(
        partner_id=partner_id, token_hash=hash_token(secret), prefix=secret[:12], label=label,
        expires_at=datetime.now(UTC) + timedelta(days=days),
    )
    session.add(token)
    session.flush()
    return token, secret


def authenticate(session: Session, secret: str) -> Scope:
    token = session.scalar(select(ApiToken).where(
        ApiToken.token_hash == hash_token(secret),
        ApiToken.revoked_at.is_(None),
        ApiToken.expires_at > datetime.now(UTC),
    ))
    if token is None:
        raise ServiceError(401, "invalid_token", "Missing, invalid, expired, or revoked token")
    return Scope(partner_id=token.partner_id, token_id=token.id)
