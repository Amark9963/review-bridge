import logging
import time
from dataclasses import asdict
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import logs, services
from app.auth import Scope, authenticate
from app.config import settings
from app.db import get_session
from app.errors import ServiceError
from app.providers import CAPABILITIES
from app.schemas import (
    AccountOut,
    ConnectionOut,
    CreateConnection,
    CreatedConnectionOut,
    CreateNamed,
    DisableConnection,
    LocationOut,
    PartnerOut,
    ReviewFeedQuery,
    ReviewPageOut,
    ReviewSiteOut,
    SyncJobDetailOut,
    SyncJobOut,
)
from app.yelp import YelpClient, yelp_client

logs.configure(settings().log_level)
log = logging.getLogger("app.api")

app = FastAPI(title="ReviewBridge", version="0.1.0", description=(
    "Partner-scoped review integration. Provision a token through the operator CLI, then "
    "paste the token into Authorize. Yelp is synthetic: no live Yelp calls are made yet."
))
bearer = HTTPBearer(auto_error=False)
DB = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Page = Annotated[int, Query(ge=1)]


def current_scope(
    request: Request,
    session: DB,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Scope:
    if credentials is None:
        raise ServiceError(401, "invalid_token", "Bearer token required")
    scope = authenticate(session, credentials.credentials)
    # For the request log: who called, by IDs only, never the token itself.
    request.state.partner_id = scope.partner_id
    request.state.token_id = scope.token_id
    return scope


Auth = Annotated[Scope, Depends(current_scope)]
Yelp = Annotated[YelpClient, Depends(yelp_client)]


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Assigns the request ID and writes one log line per request. The path is logged
    without its query string, and the caller only by partner and token IDs."""
    request.state.request_id = str(uuid4())
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request.state.request_id
        return response
    finally:
        route = request.scope.get("route")
        # Container health checks poll every few seconds; keep them out of INFO logs.
        level = logging.DEBUG if request.url.path.startswith("/health/") else logging.INFO
        log.log(level, "request", extra={"fields": {
            "request_id": request.state.request_id, "method": request.method,
            "route": getattr(route, "path", None), "path": request.url.path,
            "status": status, "duration_ms": round((time.monotonic() - started) * 1000),
            "partner_id": getattr(request.state, "partner_id", None),
            "token_id": getattr(request.state, "token_id", None),
        }})


@app.exception_handler(ServiceError)
async def service_error(request: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message, "request_id": request.state.request_id,
    }}, headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else exc.headers)


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


@app.get("/v1/review-sites", response_model=list[ReviewSiteOut], tags=["connections"])
def list_review_sites(scope: Auth):
    return list(CAPABILITIES.values())


@app.post("/v1/locations/{location_id}/connections", response_model=CreatedConnectionOut,
          status_code=201, tags=["connections"])
def create_connection(location_id: UUID, body: CreateConnection, session: DB, scope: Auth,
                      yelp: Yelp):
    result, business = services.create_connection(session, scope, yelp, location_id,
                                                  body.provider, body.external_business_id)
    return {**ConnectionOut.model_validate(result).model_dump(), "business": asdict(business)}


@app.get("/v1/locations/{location_id}/connections", response_model=list[ConnectionOut],
         tags=["connections"])
def list_connections(location_id: UUID, session: DB, scope: Auth, limit: Limit = 25,
                     page: Page = 1, include_disabled: bool = False):
    return services.connections(session, scope, location_id, include_disabled, limit, page)


@app.get("/v1/connections/{connection_id}", response_model=ConnectionOut, tags=["connections"])
def get_connection(connection_id: UUID, session: DB, scope: Auth):
    return services.connection(session, scope, connection_id)


@app.post("/v1/connections/{connection_id}/disable", response_model=ConnectionOut,
          tags=["connections"])
def disable_connection(connection_id: UUID, body: DisableConnection, session: DB, scope: Auth):
    return services.disable_connection(session, scope, connection_id, body.reason)


@app.post("/v1/connections/{connection_id}/sync", response_model=SyncJobOut,
          status_code=202, tags=["sync"])
def request_sync(connection_id: UUID, session: DB, scope: Auth):
    return services.request_sync(session, scope, connection_id)


@app.get("/v1/connections/{connection_id}/sync-jobs", response_model=list[SyncJobOut],
         tags=["sync"])
def list_sync_jobs(connection_id: UUID, session: DB, scope: Auth, limit: Limit = 25,
                   page: Page = 1):
    return services.sync_jobs(session, scope, connection_id, limit, page)


@app.get("/v1/sync-jobs/{job_id}", response_model=SyncJobDetailOut, tags=["sync"])
def get_sync_job(job_id: UUID, session: DB, scope: Auth):
    return services.sync_job(session, scope, job_id)


FeedQuery = Annotated[ReviewFeedQuery, Query()]


@app.get("/v1/partner/reviews", response_model=ReviewPageOut, tags=["reviews"])
def partner_reviews(session: DB, scope: Auth, query: FeedQuery):
    return services.review_feed(session, scope, query)


@app.get("/v1/accounts/{account_id}/reviews", response_model=ReviewPageOut, tags=["reviews"])
def account_reviews(account_id: UUID, session: DB, scope: Auth, query: FeedQuery):
    return services.review_feed(session, scope, query, account_id)
