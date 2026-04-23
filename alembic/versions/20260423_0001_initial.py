"""initial schema

Revision ID: 20260423_0001
Revises:
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa

revision = "20260423_0001"
down_revision = None
branch_labels = None
depends_on = None


UUID = sa.Uuid(as_uuid=True)


def uuid_col() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True, nullable=False)


def upgrade() -> None:
    op.create_table(
        "providers",
        uuid_col(),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_providers_name", "providers", ["name"], unique=True)

    op.create_table(
        "provider_endpoints",
        uuid_col(),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reachable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_provider_endpoints_host", "provider_endpoints", ["host"])
    op.create_index("ix_provider_endpoints_provider_name", "provider_endpoints", ["provider_name"])
    op.create_index("ix_provider_endpoints_endpoint", "provider_endpoints", ["endpoint"], unique=True)

    op.create_table(
        "managed_nodes",
        uuid_col(),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_node_id", sa.String(length=128), nullable=False),
        sa.Column("node_num", sa.Integer(), nullable=True),
        sa.Column("short_name", sa.String(length=64), nullable=True),
        sa.Column("long_name", sa.String(length=128), nullable=True),
        sa.Column("firmware_version", sa.String(length=128), nullable=True),
        sa.Column("hardware_model", sa.String(length=128), nullable=True),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("managed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("altitude", sa.Float(), nullable=True),
        sa.Column("location_source", sa.String(length=64), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_sync", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reachable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("capability_matrix", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.UniqueConstraint("provider", "provider_node_id", name="uq_provider_node"),
    )
    op.create_index("ix_managed_nodes_provider", "managed_nodes", ["provider"])
    op.create_index("ix_managed_nodes_provider_node_id", "managed_nodes", ["provider_node_id"])
    op.create_index("ix_managed_nodes_reachable", "managed_nodes", ["reachable"])
    op.create_index("ix_managed_nodes_last_seen", "managed_nodes", ["last_seen"])

    op.create_table(
        "node_endpoints",
        uuid_col(),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_node_endpoints_node_id", "node_endpoints", ["node_id"])
    op.create_index("ix_node_endpoints_endpoint", "node_endpoints", ["endpoint"])

    op.create_table(
        "node_groups",
        uuid_col(),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("dynamic_filter", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("desired_config_template", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_node_groups_name", "node_groups", ["name"], unique=True)

    op.create_table(
        "node_group_members",
        uuid_col(),
        sa.Column("group_id", UUID, sa.ForeignKey("node_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("group_id", "node_id", name="uq_group_node"),
    )

    op.create_table(
        "node_tags",
        uuid_col(),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("node_id", "tag", name="uq_node_tag"),
    )

    op.create_table(
        "node_snapshots",
        uuid_col(),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "node_configs",
        uuid_col(),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("config_type", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "node_desired_state",
        uuid_col(),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("desired_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("drift_state", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "jobs",
        uuid_col(),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "job_results",
        uuid_col(),
        sa.Column("job_id", UUID, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "audit_logs",
        uuid_col(),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("node_id", UUID, sa.ForeignKey("managed_nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("group_id", UUID, sa.ForeignKey("node_groups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("before_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("after_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        uuid_col(),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    for table in [
        "users",
        "audit_logs",
        "job_results",
        "jobs",
        "node_desired_state",
        "node_configs",
        "node_snapshots",
        "node_tags",
        "node_group_members",
        "node_groups",
        "node_endpoints",
        "managed_nodes",
        "provider_endpoints",
        "providers",
    ]:
        op.drop_table(table)
