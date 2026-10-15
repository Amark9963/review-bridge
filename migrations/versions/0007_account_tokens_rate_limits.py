"""Account-scoped API tokens and per-token rate-limit counters."""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("api_tokens", sa.Column("account_id", sa.Uuid()))
    op.create_foreign_key("fk_api_tokens_account_tenant", "api_tokens", "accounts",
                          ["partner_id", "account_id"], ["partner_id", "id"])
    op.create_index("ix_api_tokens_account_id", "api_tokens", ["account_id"])
    op.create_table("rate_limit_counters",
        sa.Column("token_id", sa.Uuid(), sa.ForeignKey("api_tokens.id"), primary_key=True),
        sa.Column("bucket", sa.String(16), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("rate_limit_counters")
    # Account tokens cannot exist without the column; revoke rather than widen them.
    op.execute("DELETE FROM api_tokens WHERE account_id IS NOT NULL")
    op.drop_index("ix_api_tokens_account_id", "api_tokens")
    op.drop_constraint("fk_api_tokens_account_tenant", "api_tokens", type_="foreignkey")
    op.drop_column("api_tokens", "account_id")
