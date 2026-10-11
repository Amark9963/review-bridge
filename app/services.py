from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Scope
from app.errors import not_found
from app.models import Account, Location, Partner


def partner(session: Session, scope: Scope):
    result = session.get(Partner, scope.partner_id)
    if result is None:
        raise not_found()
    return result


def account(session: Session, scope: Scope, account_id: UUID):
    result = session.scalar(select(Account).where(
        Account.id == account_id, Account.partner_id == scope.partner_id,
    ))
    if result is None:
        raise not_found()
    return result


def location(session: Session, scope: Scope, location_id: UUID):
    result = session.scalar(select(Location).where(
        Location.id == location_id, Location.partner_id == scope.partner_id,
    ))
    if result is None:
        raise not_found()
    return result


def create_account(session: Session, scope: Scope, partner_id: UUID, name: str):
    if partner_id != scope.partner_id:
        raise not_found()
    result = Account(partner_id=scope.partner_id, name=name)
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def create_location(session: Session, scope: Scope, account_id: UUID, name: str):
    account(session, scope, account_id)
    result = Location(partner_id=scope.partner_id, account_id=account_id, name=name)
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def accounts(session: Session, scope: Scope, limit: int, page: int):
    return session.scalars(select(Account).where(Account.partner_id == scope.partner_id)
        .order_by(Account.created_at, Account.id).limit(limit).offset((page - 1) * limit)).all()


def locations(session: Session, scope: Scope, account_id: UUID, limit: int, page: int):
    account(session, scope, account_id)
    return session.scalars(select(Location).where(
        Location.partner_id == scope.partner_id, Location.account_id == account_id,
    ).order_by(Location.created_at, Location.id).limit(limit).offset((page - 1) * limit)).all()
