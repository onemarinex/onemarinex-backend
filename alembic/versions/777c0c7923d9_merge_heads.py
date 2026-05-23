"""merge heads

Revision ID: 777c0c7923d9
Revises: 3c2a7f6a1d4b, dd5d43e59e0e
Create Date: 2026-05-22 12:32:18.387128

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '777c0c7923d9'
down_revision: Union[str, None] = ('3c2a7f6a1d4b', 'dd5d43e59e0e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
