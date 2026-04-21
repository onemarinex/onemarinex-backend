"""add operating_port_id fk

Revision ID: 08831e872be0
Revises: 215d263f2b27
Create Date: 2026-04-21 01:04:18.742836

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '08831e872be0'
down_revision: Union[str, None] = '215d263f2b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # 1. ✅ Add new column
    op.execute("""
        ALTER TABLE aggregator_profiles
        ADD COLUMN IF NOT EXISTS operating_port_id INTEGER
    """)

    # 2. ✅ Copy data (ONLY if old column exists)
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='aggregator_profiles' 
            AND column_name='operating_port'
        ) THEN
            UPDATE aggregator_profiles ap
            SET operating_port_id = p.id
            FROM ports p
            WHERE ap.operating_port = p.name;
        END IF;
    END$$;
    """)

    # 3. ✅ Add FK safely
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint 
            WHERE conname = 'fk_operating_port'
        ) THEN
            ALTER TABLE aggregator_profiles
            ADD CONSTRAINT fk_operating_port
            FOREIGN KEY (operating_port_id)
            REFERENCES ports(id);
        END IF;
    END$$;
    """)

    # 4. ✅ Drop old column safely
    op.execute("""
        ALTER TABLE aggregator_profiles
        DROP COLUMN IF EXISTS operating_port
    """)


def downgrade():
    # reverse (basic version)

    op.execute("""
        ALTER TABLE aggregator_profiles
        ADD COLUMN operating_port VARCHAR(255)
    """)

    # optional: reverse mapping
    op.execute("""
        UPDATE aggregator_profiles ap
        SET operating_port = p.name
        FROM ports p
        WHERE ap.operating_port_id = p.id
    """)

    op.execute("""
        ALTER TABLE aggregator_profiles
        DROP CONSTRAINT IF EXISTS fk_operating_port
    """)

    op.execute("""
        ALTER TABLE aggregator_profiles
        DROP COLUMN IF EXISTS operating_port_id
    """)