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
  by `YELP_MODE`; the live implementation arrives with authorized access (slice 5).
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

- Partners trigger a sync with `POST /v1/connections/{id}/sync`. This differs from SAU,
  which has no on-demand collection endpoint. Until inbound rate limits exist, a cooldown
  per connection (`SYNC_COOLDOWN_SECONDS`, default 300) protects provider quota: the slot
  is claimed atomically before the provider call, a repeat within the cooldown gets 429
  `sync_cooldown` with `Retry-After`, and a provider outage gives the slot back.
- Order: claim the slot, end the transaction, fetch outside any transaction, normalize, then
  lock the connection row, re-check it is active, and write. Disabling updates the same row,
  so a disable during the fetch means nothing is written (409 `connection_disabled`).
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

## Next slices

1. ~~Location review-site connections and capabilities (Yelp only).~~ Done; see Connections.
2. ~~Synthetic Yelp adapter response -> normalization -> conditional upsert -> review
   feed.~~ Done; see Reviews and feed.
3. Durable PostgreSQL jobs, worker leases/recovery, attempt history, retries and health.
4. Feed filters, updated_since, page validation, eventual-consistency contract.
5. Authorized live Yelp evaluation with content expiry and no unsupported completeness claims.
6. Scheduling, inbound rate limits, account tokens and operational logging.
7. Optional signed outbound webhooks with stable event IDs and durable deduplication.

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
