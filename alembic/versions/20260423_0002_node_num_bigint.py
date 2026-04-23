"""managed_nodes.node_num bigint

Revision ID: 20260423_0002
Revises: 20260423_0001
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa

revision = "20260423_0002"
down_revision = "20260423_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("managed_nodes") as batch_op:
        batch_op.alter_column(
            "node_num",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("managed_nodes") as batch_op:
        batch_op.alter_column(
            "node_num",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
