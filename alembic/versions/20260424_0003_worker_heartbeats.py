"""worker heartbeat table

Revision ID: 20260424_0003
Revises: 20260423_0002
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260424_0003"
down_revision = "20260423_0002"
branch_labels = None
depends_on = None


UUID = sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="external"),
        sa.Column("host", sa.String(length=128), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_claimed_job_id", UUID, sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_completed_job_id", UUID, sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("processed_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_worker_heartbeats_mode", "worker_heartbeats", ["mode"])
    op.create_index("ix_worker_heartbeats_host", "worker_heartbeats", ["host"])
    op.create_index("ix_worker_heartbeats_pid", "worker_heartbeats", ["pid"])
    op.create_index("ix_worker_heartbeats_status", "worker_heartbeats", ["status"])
    op.create_index("ix_worker_heartbeats_last_heartbeat_at", "worker_heartbeats", ["last_heartbeat_at"])


def downgrade() -> None:
    op.drop_table("worker_heartbeats")

