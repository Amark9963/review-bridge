"""Record what triggered each sync job, for scheduled syncing."""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sync_jobs", sa.Column("trigger", sa.String(16)))
    # Each connection's first job was queued when it was connected; later ones were requested.
    op.execute("""
        UPDATE sync_jobs SET trigger = CASE WHEN id = (
            SELECT first.id FROM sync_jobs first
            WHERE first.connection_id = sync_jobs.connection_id
            ORDER BY first.created_at, first.id LIMIT 1
        ) THEN 'connect' ELSE 'manual' END
    """)
    op.alter_column("sync_jobs", "trigger", nullable=False)
    op.create_check_constraint("ck_sync_jobs_trigger", "sync_jobs",
                               "trigger IN ('connect', 'manual', 'schedule')")


def downgrade():
    op.drop_constraint("ck_sync_jobs_trigger", "sync_jobs", type_="check")
    op.drop_column("sync_jobs", "trigger")
