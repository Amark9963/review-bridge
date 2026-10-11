"""Initial partner hierarchy and hashed partner tokens."""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def identity():
    return [sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now())]


def upgrade():
    op.create_table("partners", *identity(), sa.Column("name", sa.String(200), nullable=False))
    op.create_table("accounts", *identity(),
        sa.Column("partner_id", sa.Uuid(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.UniqueConstraint("partner_id", "id", name="uq_accounts_partner_id_id"))
    op.create_index("ix_accounts_partner_id", "accounts", ["partner_id"])
    op.create_table("locations", *identity(),
        sa.Column("partner_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(["partner_id", "account_id"],
                                ["accounts.partner_id", "accounts.id"],
                                name="fk_locations_account_tenant"))
    op.create_index("ix_locations_partner_id", "locations", ["partner_id"])
    op.create_index("ix_locations_account_id", "locations", ["account_id"])
    op.create_table("api_tokens", *identity(),
        sa.Column("partner_id", sa.Uuid(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_api_tokens_partner_id", "api_tokens", ["partner_id"])


def downgrade():
    op.drop_table("api_tokens")
    op.drop_table("locations")
    op.drop_table("accounts")
    op.drop_table("partners")
