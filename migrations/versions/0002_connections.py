"""Yelp review-site connections for locations."""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ACTIVE = sa.text("disabled_at IS NULL")


def upgrade():
    op.create_unique_constraint("uq_locations_partner_id_id", "locations", ["partner_id", "id"])
    op.create_table("review_site_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("partner_id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_business_id", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_reason", sa.String(32)),
        sa.ForeignKeyConstraint(["partner_id", "location_id"],
                                ["locations.partner_id", "locations.id"],
                                name="fk_connections_location_tenant"),
        sa.CheckConstraint("provider IN ('yelp')", name="ck_connections_provider"),
        sa.CheckConstraint("disabled_reason IN ('incorrect_business', 'no_longer_needed')",
                           name="ck_connections_disabled_reason"),
        sa.CheckConstraint("(disabled_at IS NULL) = (disabled_reason IS NULL)",
                           name="ck_connections_disabled_consistent"))
    op.create_index("ix_review_site_connections_location_id", "review_site_connections",
                    ["location_id"])
    op.create_index("uq_connections_active_location", "review_site_connections",
                    ["location_id", "provider"], unique=True, postgresql_where=ACTIVE)
    op.create_index("uq_connections_active_business", "review_site_connections",
                    ["partner_id", "provider", "external_business_id"], unique=True,
                    postgresql_where=ACTIVE)


def downgrade():
    op.drop_table("review_site_connections")
    op.drop_constraint("uq_locations_partner_id_id", "locations", type_="unique")
