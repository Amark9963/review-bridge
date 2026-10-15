"""Operator-only provisioning: never exposed as an unauthenticated API route."""
import argparse
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.auth import issue_token
from app.config import settings
from app.db import engine
from app.models import ApiToken, Partner, ReviewSiteConnection, SyncJob, WorkerHeartbeat
from app.schemas import CreateNamed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("create-partner")
    bootstrap.add_argument("--name", required=True)
    issue = commands.add_parser("issue-token")
    issue.add_argument("--partner-id", required=True, type=UUID)
    issue.add_argument("--label", default="operator-issued")
    revoke = commands.add_parser("revoke-token")
    revoke.add_argument("--token-id", required=True, type=UUID)
    commands.add_parser("job-stats")
    args = parser.parse_args()
    with Session(engine()) as session:
        if args.command == "job-stats":
            print(json.dumps(job_stats(session), indent=2))
            return
        if args.command == "revoke-token":
            token = session.get(ApiToken, args.token_id)
            if token is None:
                parser.error("Token not found")
            token.revoked_at = datetime.now(UTC)
            session.commit()
            print("Token revoked")
            return
        if args.command == "create-partner":
            partner = Partner(name=CreateNamed(name=args.name).name)
            session.add(partner)
            session.flush()
            label = "bootstrap"
        else:
            partner = session.get(Partner, args.partner_id)
            if partner is None:
                parser.error("Partner not found")
            label = CreateNamed(name=args.label).name
        token, secret = issue_token(session, partner.id, label)
        result = {"partner_id": str(partner.id), "token_id": str(token.id),
                  "token": secret, "expires_at": token.expires_at.isoformat()}
        session.commit()
        print(json.dumps(result, indent=2))


def job_stats(session: Session) -> dict:
    """A growing oldest_due_seconds is the first sign that workers are stuck or missing;
    connections_overdue counts connections the scheduler should have queued by now."""
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    due = SyncJob.status == "queued", SyncJob.run_after <= now
    oldest = session.scalar(select(func.min(SyncJob.run_after)).where(*due))

    def count(*conditions):
        return session.scalar(select(func.count()).select_from(SyncJob).where(*conditions))

    return {
        "queued_due": count(*due),
        "queued_later": count(SyncJob.status == "queued", SyncJob.run_after > now),
        "oldest_due_seconds": int((now - oldest).total_seconds()) if oldest else 0,
        "running": count(SyncJob.status == "running"),
        "succeeded_24h": count(SyncJob.status == "succeeded", SyncJob.finished_at >= day_ago),
        "failed_24h": count(SyncJob.status == "failed", SyncJob.finished_at >= day_ago),
        "connections_overdue": overdue(session, now),
        "workers_alive": session.scalar(select(func.count()).select_from(WorkerHeartbeat)
            .where(WorkerHeartbeat.last_seen_at >= now - timedelta(seconds=30))),
    }


def overdue(session: Session, now: datetime) -> int:
    """Active, schedulable connections with no job for an hour past their interval."""
    since = now - timedelta(hours=settings().sync_interval_hours + 1)
    recent = exists().where(SyncJob.connection_id == ReviewSiteConnection.id,
                            SyncJob.created_at > since)
    return session.scalar(select(func.count()).select_from(ReviewSiteConnection).where(
        ReviewSiteConnection.disabled_at.is_(None),
        ReviewSiteConnection.sync_status != "needs_attention", ~recent))


if __name__ == "__main__":
    main()
