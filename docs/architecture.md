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

## Next slices

1. Location review-site connections and capabilities (Yelp only).
2. Synthetic Yelp adapter response -> normalization -> conditional upsert -> review feed.
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
