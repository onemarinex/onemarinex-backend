from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision: str = 'dd5d43e59e0e'
down_revision: Union[str, None] = '08831e872be0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ✅ IMPORTANT: do NOT define values here
place_category_enum = postgresql.ENUM(
    name='placecategory',
    create_type=False
)


def upgrade() -> None:
    # ✅ 1. Ensure ENUM exists (safe)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'placecategory'
        ) THEN
            CREATE TYPE placecategory AS ENUM ('restaurant', 'pub', 'hotel', 'sightseeing');
        END IF;
    END$$;
    """)

    # ✅ 2. Create table ONLY if not exists
    op.execute("""
    CREATE TABLE IF NOT EXISTS vendors (
        id SERIAL PRIMARY KEY,
        port_id INTEGER REFERENCES ports(id),
        name VARCHAR NOT NULL,
        category placecategory NOT NULL,
        location_name VARCHAR NOT NULL,
        distance_from_port FLOAT NOT NULL,
        rating FLOAT,
        lat FLOAT NOT NULL,
        lng FLOAT NOT NULL,
        phone VARCHAR,
        email VARCHAR,
        status VARCHAR(32) DEFAULT 'Active',
        documents JSONB,
        images JSONB,
        other_information JSONB,
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL
    );
    """)


def downgrade() -> None:
    # ⚠️ Drop table safely
    op.execute("DROP TABLE IF EXISTS vendors")

    # ⚠️ Drop ENUM only if not used anywhere
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_type t
            JOIN pg_enum e ON t.oid = e.enumtypid
            JOIN pg_depend d ON d.objid = t.oid
            WHERE t.typname = 'placecategory'
        ) THEN
            DROP TYPE IF EXISTS placecategory;
        END IF;
    END$$;
    """)