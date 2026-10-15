# Architecture decisions

Approved by the user on 2026-10-11. Implement in vertical slices, explaining important
choices as they arise. No frontend, billing, reply posting, other providers, Redis,
Kafka, Kubernetes, or microservices in V1.

## Foundation

- Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL 17, Docker Compose.
- One codebase. API now; a separate worker process from the same codebase with sync jobs.
- Shared tenant tables. Services take a trusted Scope derived from a hashed opaque token.
- Partner IDs never come from creation request bodies. Path IDs are authorized in services.
- Foreign keys enforce account/location tenant consistency. No implicit cascading deletion.
- Unknown and inaccessible resources both return 404; missing/invalid tokens return 401.
- Partner bootstrap and token issuance/revocation are operator CLI actions.
- Synchronous SQLAlchemy keeps transaction ownership understandable at this scale.
- This first slice supports partner tokens only. Account scope is a later migration.
- Development and test databases are separate; integration tests exercise PostgreSQL.

## Connections

Approved by the user on 2026-10-11 and implemented in the second slice.

- A connection links one location to one external business on one provider (Yelp only).
- Yelp business IDs only (22 URL-safe characters); aliases and URLs are rejected.
- Verification happens during the create request, not in a background job, so every stored
  connection is verified. Order: authorize the location and check conflicts (so failing
  requests spend no provider quota), end the database transaction, look the ID up with a
  short timeout, then insert. Unknown IDs return 422 `business_not_found`; provider
  timeouts, errors and rate limits return 503 `provider_unavailable` with `Retry-After`.
  Nothing is stored on failure. The trade-off: connecting is unavailable while Yelp is.
- The Yelp adapter has a synthetic implementation (invented businesses, no network) selected
  by `YELP_MODE`. A live-ready implementation is built in slice 5 but never switched on
  (see Live Yelp decision).
- Yelp's name, address and URL are returned once in the create response for the partner to
  confirm the business, and never stored, until Yelp's caching terms are resolved.
- One active connection per provider per location, and within a partner one active location
  per business. Uniqueness is not enforced across partners, which would leak tenant data.
  Both rules are partial unique indexes; conflicts return 409.
- Connections are disabled, never deleted, and the business ID cannot be changed. Correcting
  it means disabling the connection and creating another.
- Disabling requires a reason: `incorrect_business` (its reviews were never valid) or
  `no_longer_needed` (correct reviews, collection stops). Repeating a disable keeps the
  original reason. Accidental disables are permanent; reconnect instead.
- Provider capabilities are declared in code and served at `GET /v1/review-sites`, so
  partners see Yelp's limits (three-excerpt sample, no history, 24-hour content age) upfront.
- If shared listings are needed later, dropping the business index allows them, but sync
  must then share fetches between connections to the same business.

Agreed for later slices:

- Reviews stay with the connection that fetched them and never move to a new connection.
  The feed shows only active connections' reviews; disabled ones expire with live content.
- The feed must announce removals with the disable reason, so partners who already copied
  reviews (especially `incorrect_business` ones) can delete them. It should include
  `provider` and `external_review_id` so partners can recognise reviews after a reconnect.
- Sync failures, closures and Yelp listing merges are a sync status (for example
  `needs_attention`), never an automatic disable. A merge never changes the stored ID.
- Disabling cancels scheduled syncs, and a running sync re-checks the connection is still
  active before writing.
- Deactivating a location or account disables its connections explicitly, not by cascade.
- Compliance removal of review content is a separate operator CLI action.

## Reviews and feed

Approved by the user on 2026-10-11 and implemented in the third slice.

- Partners can request a sync with `POST /v1/connections/{id}/sync`. This differs from SAU,
  which has no on-demand collection endpoint. Since the fourth slice it queues a job (see
  Sync jobs and worker) rather than calling the provider during the request.
- The adapter returns raw Yelp-shaped reviews so normalization is exercised against the live
  response shape. Bad entries are rejected individually and counted; repeated IDs keep the
  first. Text is capped at 160 characters (`excerpt_max_chars`). Only the reviewer's display
  name is stored, never profile or photo URLs.
- Yelp's reference does not state the timezone of `time_created`; UTC is assumed in one
  place (`app/reviews.py`) and must be verified against live data.
- Writes compare a content hash: new reviews are inserted, changed ones update content and
  `content_updated_at`, unchanged ones only advance `last_seen_at`. Missing reviews are kept.
- Feeds follow SAU's scoping: `GET /v1/partner/reviews` (whole partner) and
  `GET /v1/accounts/{id}/reviews` (one account, which account tokens will use later). Both
  show only active connections, newest first, with `meta` (page, limit, total, last_page).
  Filters, removals and cursor sync were added in the fifth slice (see Feed filters and
  incremental sync).

## Sync jobs and worker

Approved by the user on 2026-10-11 and implemented in the fourth slice. This moves
collection into the background, as SAU's public behaviour implies (queued first collection,
scheduled collection, no on-demand endpoint); SAU's internals are not public.

- Jobs live in PostgreSQL (`sync_jobs`) with one row per attempt (`sync_attempts`). A
  partial unique index allows one queued or running job per connection.
- `POST /v1/connections/{id}/sync` returns 202 with the queued job; while one is unfinished
  it returns that job, so repeats are harmless. A cooldown since the last job was created
  (`SYNC_COOLDOWN_SECONDS`, default 300) still applies; repeats get 429 `sync_cooldown`.
- Creating a connection queues its first sync in the same transaction.
- Disabling cancels queued jobs. A running job re-checks the connection, under its row
  lock, before writing, and is cancelled instead if it was disabled.
- The worker (`python -m app.worker`, Compose service `worker`) claims the oldest due job
  with `FOR UPDATE SKIP LOCKED` under a 60-second lease, fetches outside any transaction,
  then writes only if it still holds the lease. Several workers can run; one is deployed.
- Expired leases (a crashed or killed worker) return the job to the queue with the attempt
  recorded as `lease_expired`. Jobs are at least once; review writes are idempotent.
- Retryable errors (provider unavailable, unexpected errors) back off 30 s, 2 min, 8 min and
  30 min (each +/-20%) for up to 5 attempts, then fail. `business_not_found` fails at once.
- Connections carry `sync_status`: `pending`, `ok`, `failing` (retrying or retries
  exhausted) or `needs_attention` (permanent error), plus `last_sync_error`. Failures never
  disable a connection.
- Partners see jobs and attempt history at `GET /v1/sync-jobs/{id}` and
  `GET /v1/connections/{id}/sync-jobs`; worker identities are not shown.
- Health: workers record heartbeats; the Compose health check (`--check`) requires a recent
  one. The operator command `job-stats` reports queue depth, the oldest due job's age, and
  24-hour outcomes. The worker finishes its current job on SIGTERM.
- Workers poll every 2 seconds when idle; PostgreSQL LISTEN/NOTIFY can replace this later.
- Disabling cancels queued jobs with one conditional `UPDATE ... WHERE status = 'queued'`,
  so a job a worker claims at the same moment is left running and cancels itself.
- The lease is not renewed during the provider call. The synthetic adapter returns at once;
  the live-ready adapter (slice 5) must keep its timeout well under the 60-second lease or
  renew it, otherwise a slow call can be recovered and fetched twice (writes stay safe).

## Feed filters and incremental sync

Approved by the user on 2026-10-15 (cursor paging for sync; removals announced now) and
implemented in the fifth slice. Filters and `updated_since` follow SAU's feed; the cursor
differs from SAU, which documents page numbers only.

- Both feeds accept `location_id`, `provider`, repeated `rating`, `reviewed_from`/
  `reviewed_to` (review date), `created_since` (when collected) and `updated_since`.
  Timestamps need an explicit offset. Unknown parameters are rejected (422), so a typo is
  never silently ignored. A location outside the partner, or outside the account being
  read, is 404 like any other unknown ID.
- Two orders. `sort=reviewed_at` (default, newest first) is for browsing, with page numbers
  and `meta.total`. `sort=updated_at` (oldest change first) is for sync, paged by
  `meta.next_cursor`, which encodes the last row's `(updated_at, id)`.
- Why a cursor: with page numbers, a review that changes after a client read page 1 moves to
  the end, every row behind it shifts back one place, and one unchanged review is skipped
  for good. With a cursor, "the rows after this one" is unaffected; the changed review is
  simply served again at the end. `page` with `sort=updated_at` is rejected.
- Page validation: `limit` 1-100; browsing serves at most 10,000 rows deep (`page` 400 at
  the default limit); `reviewed_from` must not be after `reviewed_to`; a malformed cursor is
  422 `invalid_cursor`.
- `reviews.updated_at` advances when content changes or the review is removed, never for
  unchanged observations. It is a stored, indexed column `(partner_id, updated_at, id)`.
- Removals: `include_removed=true` also returns reviews of disabled connections, with
  `removed_at` and `removed_reason`. Disabling advances their `updated_at`, so removals
  reach `updated_since` syncs and partners can delete reviews they copied.
- Writers read the clock only after taking the connection's row lock (the worker before
  writing reviews, disabling before marking them removed), and disabling uses
  `GREATEST(updated_at, now)`, so a review's `updated_at` never moves backwards.

Freshness contract (eventual consistency): a change is stamped just before its transaction
commits, and timestamps come from the hosts' clocks. A change can therefore become visible
slightly after a client read past its timestamp. Clients sync like this:

1. Request `sort=updated_at&include_removed=true&updated_since=<T>`, where `T` is when the
   previous sync started minus 5 minutes (omit it the first time).
2. Follow `meta.next_cursor` until it is null. Note when this sync started.
3. Upsert each review by `id`; delete any with `removed_at` set.

The overlap re-serves some reviews, which upserting makes harmless. Five minutes covers
commit delay and modest clock skew between hosts; it is not a hard guarantee.

## Scheduling and logging

Implemented in slice 6a on 2026-10-16 with defaults proposed to the user (no API contract
changes beyond a new `trigger` field on jobs).

- The worker schedules once a minute: every active connection with no job in the last
  `SYNC_INTERVAL_HOURS` (default 12; Yelp content may be kept 24 hours, so one missed run
  still fits) gets a job. Connections in `needs_attention` are skipped, since retrying a
  business the provider no longer knows only spends quota; a successful partner-requested
  sync returns them to the schedule. Disabled connections are never scheduled.
- Scheduling is one `INSERT ... SELECT ... ON CONFLICT DO NOTHING` on the unfinished-job
  index, so it never duplicates a queued, running or concurrently requested job, and
  several workers can schedule at once without coordination. Up to 500 jobs per run.
- Scheduled jobs start at random within 5 minutes, so a batch (for example after a restart)
  does not reach the provider at once.
- Jobs record a `trigger`: `connect`, `manual` or `schedule`.
- Logs are JSON, one object per line, through one formatter for the API, the worker and
  uvicorn. Context goes in `extra={"fields": ...}`.
- The API logs one line per request: request ID, method, route template, path without the
  query string, status, duration, and the caller's partner and token IDs. Never the token,
  the Authorization header, query strings or review content. Health checks log at DEBUG.
  uvicorn's access log is disabled in favour of this.
- The worker logs each sync attempt (job, connection, partner, attempt, trigger, outcome,
  error code, counts, duration), each scheduling run that queued something, lease recovery
  and lost leases.
- `job-stats` adds `connections_overdue`: schedulable connections with no job for an hour
  past their interval, the signal that scheduling has stopped.
- Settings reach containers only through `compose.yaml`; `.env` is not in the image. Until
  this slice that included `SYNC_COOLDOWN_SECONDS`, which the README described as
  configurable but which had no effect in Compose.

## Next slices

1. ~~Location review-site connections and capabilities (Yelp only).~~ Done; see Connections.
2. ~~Synthetic Yelp adapter response -> normalization -> conditional upsert -> review
   feed.~~ Done; see Reviews and feed.
3. ~~Durable PostgreSQL jobs, worker leases/recovery, attempt history, retries and
   health.~~ Done; see Sync jobs and worker.
4. ~~Feed filters, updated_since, page validation, eventual-consistency contract.~~ Done;
   see Feed filters and incremental sync.
5. Live-ready Yelp adapter, never switched on (see Live Yelp decision). Done after slice 6.
6. Scheduling, inbound rate limits, account tokens and operational logging, in two parts:
   ~~6a scheduling and structured logging~~ (done; see Scheduling and logging); 6b account
   tokens and inbound rate limits.
7. Optional signed outbound webhooks with stable event IDs and durable deduplication.

### Live Yelp decision

Decided by the user on 2026-10-16. ReviewBridge is a learning and portfolio project, so it
stays on the synthetic adapter and never calls the real Yelp API. Live access would add
cost (an eligible, possibly paid plan), compliance work, and a secret to manage, for data
that could not be shown publicly and that reviewers could not run themselves.

Slice 5 instead builds the live adapter as it would ship, and tests it without Yelp:

- An HTTP adapter behind `YELP_MODE=live`, with request timeouts well under the worker's
  60-second lease and an explicit retry/fail rule for each response (401, 404, 429 with
  `Retry-After`, 5xx, timeouts, malformed bodies).
- Tests against a fake Yelp server (`httpx` mock transport), not the network.
- The API key never appears in logs, errors, or the repository.
- 24-hour content expiry, demonstrated on synthetic data.
- Documentation stating plainly that it has never run against Yelp, and what a live
  evaluation would have to confirm (for example the timezone of `time_created`).

Review identity will be (connection_id, external_review_id). Unchanged observations
must not advance the review's content updated_at. Missing sample entries are not deletions.
Connection external-business identity will be immutable; correcting it creates a new connection.
Jobs will be at-least-once with idempotent effects and a single unfinished job per connection.
Outbound provider calls happen outside database transactions.

## Research boundaries

These are public contracts, not knowledge of SAU's internal implementation.

- [SAU architecture and collection](https://docs.shoutaboutus.com/docs/build-third-party-review-management)
- [SAU authentication](https://docs.shoutaboutus.com/docs/rm-authentication)
- [SAU review feed](https://docs.shoutaboutus.com/docs/rm-post-partner-report-review-response-feed)
- [SAU connection update semantics](https://docs.shoutaboutus.com/docs/rm-post-store-review-site-update)
- [SAU limits](https://docs.shoutaboutus.com/docs/rate-limits)
- [SAU webhook catalogue](https://docs.shoutaboutus.com/docs/rm-webhook-event-catalogue)
- [Yelp public reviews and eligible plans](https://docs.developer.yelp.com/reference/v3_business_reviews)
- [Yelp authentication](https://docs.developer.yelp.com/docs/places-authentication)
- [Yelp quotas and caching guidance](https://docs.developer.yelp.com/docs/places-rate-limiting)
- [Yelp restricted partner reviews](https://docs.developer.yelp.com/docs/private-reviews-api)

Yelp's public reference describes up to three excerpts despite listing larger pagination
parameters. Do not infer exhaustive access. Quotas depend on the actual app entitlement.
Caching guidance describes a 24-hour limit; full terms/display pages were inaccessible
during research, so redistribution permission remains unresolved. Yelp keys are separate
from ReviewBridge tokens. Use separate synthetic and live environments.
