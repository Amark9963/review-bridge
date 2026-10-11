# ReviewBridge

A learning and portfolio API for multi-tenant review integration, inspired by public
SAU integration patterns. Independent project; not affiliated with SAU or Yelp.

## Current slice

Implemented: PostgreSQL migrations, partner API tokens, scoped account/location creation
and reads, Swagger, health checks, and PostgreSQL integration tests.

Not implemented yet: Yelp connections, reviews, sync jobs, worker, scheduling, account
tokens, rate limiting, or webhooks. Build these one vertical slice at a time.

## Start (Docker Desktop with Linux containers)

From this directory:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps -a
```

Open http://localhost:8000/docs. Only the API is published, on localhost; PostgreSQL
is internal to Compose. Set `API_PORT` in `.env` if 8000 is occupied.
The database health check runs first, then the migration service, then the API.
An exited migration container with exit code 0 is expected.

## Create your first partner

```powershell
docker compose exec api python -m app.cli create-partner --name Northwind
```

Save the returned token securely: it is printed once and stored only as a SHA-256 hash.
Tokens contain 256 bits of randomness and expire after 90 days. Do not commit tokens.
The command creates a new partner each time; it is not an idempotent seed command.

In Swagger, choose **Authorize**, paste the raw `rb_...` token, then:

1. `GET /v1/partner` to discover your partner ID.
2. `POST /v1/partners/{partner_id}/accounts` with `{"name":"Brightwell Dental"}`.
3. `POST /v1/accounts/{account_id}/locations` with `{"name":"Georgetown"}`.
4. Read the account and its locations.
5. Create a second partner/token and try the first partner's IDs: expect `404`.

List endpoints return arrays with `page` (default 1) and `limit` (default 25, maximum 100).
These are current onboarding contracts; the future review feed will have pagination metadata.

## Token operations

```powershell
docker compose exec api python -m app.cli issue-token --partner-id <UUID> --label replacement
docker compose exec api python -m app.cli revoke-token --token-id <UUID>
```

For rotation, issue a replacement, move the client to it, then revoke the old token.
There is no unauthenticated token-issuance or partner-creation HTTP endpoint.

## Verify

```powershell
docker compose --profile test run --build --rm test
```

Tests use an isolated temporary PostgreSQL database (`reviewbridge_test`), apply the
real migrations, check model/migration alignment, exercise tenant and token boundaries,
and run Ruff. They refuse the development database and do not contact Yelp.

## Operate locally

```powershell
docker compose logs api migrate
docker compose stop
docker compose up -d
```

The named database volume survives container replacement and `docker compose down`.
Do not use `down -v` unless you intend to delete the local database.
After code changes, rebuild with `docker compose up --build -d`.

## Boundaries

This is a development deployment: local credentials, developer tools in the image,
no TLS termination, no inbound rate limiter yet. Keep it bound to localhost.
Application ownership checks are backed by a composite account/location foreign key;
PostgreSQL row-level security is not implemented. The DB credential is operator-level.

Live Yelp ingestion must wait for eligible API access and verification of permitted use.
Public Yelp review access is a limited excerpt sample. Do not assume full history,
deletion detection, incremental upstream fetching, or permanent storage rights.
Future durable demonstrations will use invented fixtures; live content needs expiry.

See [architecture decisions](docs/architecture.md) for the agreed direction and next slice.
