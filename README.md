# ReviewBridge

A learning and portfolio API for multi-tenant review integration, inspired by public
SAU integration patterns. Independent project; not affiliated with SAU or Yelp.

## Current slice

Implemented: PostgreSQL migrations, partner API tokens, scoped account/location creation
and reads, Yelp connections for locations with declared provider capabilities, on-demand
review sync from a synthetic Yelp adapter, partner and account review feeds with filters,
removal notices and cursor-based incremental sync, Swagger, health checks, and PostgreSQL
integration tests.

Sync jobs run in a separate worker with retries, lease recovery and attempt history. The
worker also syncs every active connection on a schedule (every 12 hours by default). The
API and worker write structured JSON logs.

Partners can issue account tokens limited to one account, and every authenticated request
is rate limited per token.

Not implemented yet: webhooks, and the live-ready Yelp adapter. ReviewBridge never calls
the real Yelp API; see the [Live Yelp decision](docs/architecture.md#live-yelp-decision).
Build these one vertical slice at a time.

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
5. `GET /v1/review-sites` to see what Yelp can and cannot provide.
6. `POST /v1/locations/{location_id}/connections` with
   `{"provider":"yelp","external_business_id":"rbtest0000000000000001"}`.
   The ID is verified before it is stored, and the response shows the matched business
   once for confirmation. Nothing contacts Yelp yet: the synthetic adapter treats IDs
   starting `rbtest` as existing, `rbdown` as an outage (503), and anything else as unknown.
7. `POST /v1/connections/{connection_id}/disable` with
   `{"reason":"incorrect_business"}` or `{"reason":"no_longer_needed"}`. Connections are
   never deleted and their business ID cannot change; to correct one, disable and reconnect.
8. Connect again. Its first sync is queued automatically and the `worker` service fetches
   the synthetic reviews within seconds; watch it with
   `GET /v1/connections/{connection_id}/sync-jobs` and `GET /v1/sync-jobs/{job_id}`.
   `POST /v1/connections/{connection_id}/sync` queues another (`202`); requesting again
   while one is unfinished returns the same job, and within five minutes of the last job
   returns `429` (set `SYNC_COOLDOWN_SECONDS` to change this). Read reviews with
   `GET /v1/partner/reviews` or `GET /v1/accounts/{account_id}/reviews`; disabled
   connections' reviews are hidden unless you pass `include_removed=true`.
9. Filter the feed, for example `?rating=4&rating=5&reviewed_from=2026-02-01T00:00:00Z`.
   To copy reviews incrementally, request `?sort=updated_at&include_removed=true`, follow
   `meta.next_cursor` until it is null, and next time add `updated_since` set to when that
   sync started minus five minutes. Reviews with `removed_at` set should be deleted.
10. Create a second partner/token and try the first partner's IDs: expect `404`.

Other list endpoints return arrays with `page` (default 1) and `limit` (default 25,
maximum 100). The review feeds return `data` and `meta`; see
[Feed filters and incremental sync](docs/architecture.md#feed-filters-and-incremental-sync)
for every parameter and the freshness contract.

## Token operations

```powershell
docker compose exec api python -m app.cli issue-token --partner-id <UUID> --label replacement
docker compose exec api python -m app.cli revoke-token --token-id <UUID>
```

For rotation, issue a replacement, move the client to it, then revoke the old token.
There is no unauthenticated token-issuance or partner-creation HTTP endpoint.

Partners issue **account tokens** for their own accounts through the API, for example to
give a customer access to only their account:

1. `POST /v1/accounts/{account_id}/tokens` with `{"label":"customer portal"}` (optionally
   `"expires_in_days"`, 1-365, default 90). The token is in the response once.
2. With it, `GET /v1/token` shows its `account_id`. It can do everything within that
   account (locations, connections, syncs, the account review feed). Other accounts' IDs
   return `404`; partner-level endpoints return `403 insufficient_scope`.
3. `GET /v1/accounts/{account_id}/tokens` lists them; `POST /v1/tokens/{token_id}/revoke`
   revokes one. An account can have at most 10 active tokens.

Every authenticated request is **rate limited per token** in three groups: review feeds
300/min, token issuance 30/min, everything else 120/min. Responses carry
`X-RateLimit-Limit` and `X-RateLimit-Remaining`; over the limit you get
`429 rate_limited` with `Retry-After`.

## Verify

```powershell
docker compose --profile test run --build --rm test
```

Tests use an isolated temporary PostgreSQL database (`reviewbridge_test`), apply the
real migrations, check model/migration alignment, exercise tenant and token boundaries,
and run Ruff. They refuse the development database and do not contact Yelp.

## Operate locally

```powershell
docker compose logs api migrate worker
docker compose exec api python -m app.cli job-stats
docker compose stop
docker compose up -d
```

Logs are JSON, one object per line: the API logs each request (request ID, route, status,
duration, partner and token IDs, never the token itself), and the worker logs each sync
attempt and each scheduling run. `job-stats` reports `connections_overdue`, connections
the scheduler should have queued by now. Settings such as `SYNC_INTERVAL_HOURS`,
`SYNC_COOLDOWN_SECONDS` and `LOG_LEVEL` go in `.env` (see `.env.example`) and take effect
after `docker compose up -d`.

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
