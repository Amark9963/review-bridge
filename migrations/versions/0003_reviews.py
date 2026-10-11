"""Reviews fetched through connections, and connection sync tracking."""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("review_site_connections",
                  sa.Column("sync_claimed_at", sa.DateTime(timezone=True)))
    op.add_column("review_site_connections",
                  sa.Column("last_synced_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint("uq_connections_partner_id_id", "review_site_connections",
                                ["partner_id", "id"])
    op.create_table("reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("partner_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_review_id", sa.String(64), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_name", sa.String(200), nullable=False),
        sa.Column("review_url", sa.String(2000), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["partner_id", "connection_id"],
                                ["review_site_connections.partner_id",
                                 "review_site_connections.id"],
                                name="fk_reviews_connection_tenant"),
        sa.UniqueConstraint("connection_id", "external_review_id",
                            name="uq_reviews_connection_external"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating"))
    op.create_index("ix_reviews_partner_reviewed", "reviews", ["partner_id", "reviewed_at"])


def downgrade():
    op.drop_table("reviews")
    op.drop_constraint("uq_connections_partner_id_id", "review_site_connections",
                       type_="unique")
    op.drop_column("review_site_connections", "last_synced_at")
    op.drop_column("review_site_connections", "sync_claimed_at")
