"""Operator-only provisioning: never exposed as an unauthenticated API route."""
import argparse
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.auth import issue_token
from app.db import engine
from app.models import ApiToken, Partner
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
    args = parser.parse_args()
    with Session(engine()) as session:
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


if __name__ == "__main__":
    main()
