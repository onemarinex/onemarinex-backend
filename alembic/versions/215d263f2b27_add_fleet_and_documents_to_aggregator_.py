from alembic import op
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = '215d263f2b27'
down_revision: Union[str, None] = '1df019db037a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade():
    op.execute("""
        ALTER TABLE aggregator_profiles
        ADD COLUMN IF NOT EXISTS fleet JSONB
    """)

    op.execute("""
        ALTER TABLE aggregator_profiles
        ADD COLUMN IF NOT EXISTS documents JSONB
    """)


def downgrade():
    op.execute("""
        ALTER TABLE aggregator_profiles
        DROP COLUMN IF EXISTS documents
    """)

    op.execute("""
        ALTER TABLE aggregator_profiles
        DROP COLUMN IF EXISTS fleet
    """)