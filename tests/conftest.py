import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import issue_token
from app.config import settings
from app.db import engine, get_session
from app.main import app
from app.models import Partner


@pytest.fixture
def session():
    # Never run integration tests against the development database.
    if make_url(settings().database_url).database != "reviewbridge_test":
        pytest.fail("Tests require the isolated reviewbridge_test database")
    with engine().connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def tenants(session):
    result = []
    for name in ["Northwind", "Contoso"]:
        partner = Partner(name=name)
        session.add(partner)
        session.flush()
        token, secret = issue_token(session, partner.id, "test")
        result.append((partner, token, secret))
    session.commit()
    return result
