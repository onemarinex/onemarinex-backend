"""Add opening_time and working_days to port_rules

Revision ID: b2c3d4e5f6a7
Revises: 2f6d1e4b8a2c
Create Date: 2026-07-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "2f6d1e4b8a2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("port_rules", sa.Column("opening_time", sa.String(length=8), nullable=True))
    op.add_column("port_rules", sa.Column("working_days", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("port_rules", "working_days")
    op.drop_column("port_rules", "opening_time")
