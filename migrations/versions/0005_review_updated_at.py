"""Review updated_at for incremental feed sync, covering edits and removals."""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reviews", sa.Column("updated_at", sa.DateTime(timezone=True)))
    # Existing reviews of disabled connections were removed when the connection was disabled.
    op.execute("""
        UPDATE reviews SET updated_at = GREATEST(reviews.content_updated_at,
                                                 c.disabled_at)
        FROM review_site_connections c WHERE c.id = reviews.connection_id
    """)
    op.alter_column("reviews", "updated_at", nullable=False)
    op.create_index("ix_reviews_partner_updated", "reviews", ["partner_id", "updated_at", "id"])


def downgrade():
    op.drop_index("ix_reviews_partner_updated", "reviews")
    op.drop_column("reviews", "updated_at")
