"""add closing time to port rules

Revision ID: 2f6d1e4b8a2c
Revises: 933a956e1535
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2f6d1e4b8a2c"
down_revision: Union[str, None] = "933a956e1535"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("port_rules", sa.Column("closing_time", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("port_rules", "closing_time")
