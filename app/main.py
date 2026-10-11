from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import services
from app.auth import Scope, authenticate
from app.db import get_session
from app.errors import ServiceError
from app.schemas import AccountOut, CreateNamed, LocationOut, PartnerOut

app = FastAPI(title="ReviewBridge", version="0.1.0", description=(
    "Slice 1: partner-scoped onboarding. Provision a token through the operator CLI, "
    "then paste the token into Authorize. Yelp ingestion is not implemented yet."
))
bearer = HTTPBearer(auto_error=False)
DB = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Page = Annotated[int, Query(ge=1)]


def current_scope(
    session: DB,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Scope:
    if credentials is None:
        raise ServiceError(401, "invalid_token", "Bearer token required")
    return authenticate(session, credentials.credentials)


Auth = Annotated[Scope, Depends(current_scope)]


@app.middleware("http")
async def request_id(request: Request, call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(ServiceError)
async def service_error(request: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message, "request_id": request.state.request_id,
    }}, headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Do not echo submitted inputs (which may contain secrets) in errors.
    return JSONResponse(status_code=422, content={"error": {
        "code": "validation_error", "message": "Request validation failed",
        "request_id": request.state.request_id,
        "fields": [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()],
    }})


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready(session: DB):
    try:
        session.execute(text("SELECT 1 FROM partners LIMIT 1"))
    except SQLAlchemyError:
        raise ServiceError(503, "not_ready", "Database or schema is unavailable") from None
    return {"status": "ready"}


@app.get("/v1/partner", response_model=PartnerOut, tags=["partner"])
def get_partner(session: DB, scope: Auth):
    return services.partner(session, scope)


@app.post("/v1/partners/{partner_id}/accounts", response_model=AccountOut,
          status_code=201, tags=["accounts"])
def create_account(partner_id: UUID, body: CreateNamed, session: DB, scope: Auth):
    return services.create_account(session, scope, partner_id, body.name)


@app.get("/v1/partner/accounts", response_model=list[AccountOut], tags=["accounts"])
def list_accounts(session: DB, scope: Auth, limit: Limit = 25, page: Page = 1):
    return services.accounts(session, scope, limit, page)


@app.get("/v1/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def get_account(account_id: UUID, session: DB, scope: Auth):
    return services.account(session, scope, account_id)


@app.post("/v1/accounts/{account_id}/locations", response_model=LocationOut,
          status_code=201, tags=["locations"])
def create_location(account_id: UUID, body: CreateNamed, session: DB, scope: Auth):
    return services.create_location(session, scope, account_id, body.name)


@app.get("/v1/accounts/{account_id}/locations", response_model=list[LocationOut],
         tags=["locations"])
def list_locations(account_id: UUID, session: DB, scope: Auth, limit: Limit = 25, page: Page = 1):
    return services.locations(session, scope, account_id, limit, page)


@app.get("/v1/locations/{location_id}", response_model=LocationOut, tags=["locations"])
def get_location(location_id: UUID, session: DB, scope: Auth):
    return services.location(session, scope, location_id)
