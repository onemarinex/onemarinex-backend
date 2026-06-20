from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = 'c4f1b2d9a7e3'
down_revision: Union[str, None] = '2f6d1e4b8a2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Move vendors.category away from the fixed enum so superadmin can use
    # dynamic categories/tags without schema changes.
    op.execute(
        """
        ALTER TABLE vendors
        ALTER COLUMN category TYPE VARCHAR(64)
        USING category::text;
        """
    )

    # Drop the enum type if nothing else still uses it.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_type t
                JOIN pg_depend d ON d.refobjid = t.oid
                WHERE t.typname = 'placecategory'
                  AND d.deptype = 'a'
            ) THEN
                DROP TYPE IF EXISTS placecategory;
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = 'placecategory'
            ) THEN
                CREATE TYPE placecategory AS ENUM ('restaurant', 'pub', 'hotel', 'sightseeing');
            END IF;
        END$$;
        """
    )

    op.execute(
        """
        ALTER TABLE vendors
        ALTER COLUMN category TYPE placecategory
        USING category::placecategory;
        """
    )
