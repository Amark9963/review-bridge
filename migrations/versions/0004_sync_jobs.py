"""Durable sync jobs, attempt history, worker heartbeats and connection sync status."""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def identity():
    return [sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now())]


def upgrade():
    op.drop_column("review_site_connections", "sync_claimed_at")
    op.add_column("review_site_connections",
                  sa.Column("sync_status", sa.String(32), nullable=False,
                            server_default="pending"))
    op.add_column("review_site_connections", sa.Column("last_sync_error", sa.String(64)))
    op.create_check_constraint(
        "ck_connections_sync_status", "review_site_connections",
        "sync_status IN ('pending', 'ok', 'failing', 'needs_attention')")
    op.execute("UPDATE review_site_connections SET sync_status = 'ok' "
               "WHERE last_synced_at IS NOT NULL")

    op.create_table("sync_jobs", *identity(),
        sa.Column("partner_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("fetched", sa.Integer()),
        sa.Column("inserted", sa.Integer()),
        sa.Column("updated", sa.Integer()),
        sa.Column("unchanged", sa.Integer()),
        sa.Column("rejected", sa.Integer()),
        sa.ForeignKeyConstraint(["partner_id", "connection_id"],
                                ["review_site_connections.partner_id",
                                 "review_site_connections.id"],
                                name="fk_sync_jobs_connection_tenant"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_sync_jobs_status"))
    op.create_index("uq_sync_jobs_unfinished", "sync_jobs", ["connection_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_index("ix_sync_jobs_due", "sync_jobs", ["run_after"],
                    postgresql_where=sa.text("status = 'queued'"))
    op.create_index("ix_sync_jobs_connection_created", "sync_jobs",
                    ["connection_id", "created_at"])

    op.create_table("sync_attempts", *identity(),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("sync_jobs.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("job_id", "number", name="uq_sync_attempts_job_number"),
        sa.CheckConstraint(
            "outcome IN ('running', 'succeeded', 'retrying', 'failed', 'cancelled', "
            "'lease_expired')", name="ck_sync_attempts_outcome"))

    op.create_table("worker_heartbeats",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("worker_heartbeats")
    op.drop_table("sync_attempts")
    op.drop_table("sync_jobs")
    op.drop_constraint("ck_connections_sync_status", "review_site_connections", type_="check")
    op.drop_column("review_site_connections", "last_sync_error")
    op.drop_column("review_site_connections", "sync_status")
    op.add_column("review_site_connections",
                  sa.Column("sync_claimed_at", sa.DateTime(timezone=True)))
