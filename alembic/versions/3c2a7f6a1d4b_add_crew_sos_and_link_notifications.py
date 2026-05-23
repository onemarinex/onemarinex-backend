"""Add crew sos requests and link notifications

Revision ID: 3c2a7f6a1d4b
Revises: 1df019db037a
Create Date: 2026-05-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3c2a7f6a1d4b"
down_revision: Union[str, None] = "1df019db037a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS crew_sos_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            crew_profile_id INTEGER NOT NULL REFERENCES crew_profiles(id) ON DELETE CASCADE,
            port_name VARCHAR(128),
            vessel VARCHAR(128),
            lat DOUBLE PRECISION,
            lng DOUBLE PRECISION,
            status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ,
            acknowledged_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ
        );
        """
    )

    op.execute(
        """
        ALTER TABLE notifications
        ADD COLUMN IF NOT EXISTS sos_id INTEGER REFERENCES crew_sos_requests(id) ON DELETE SET NULL;
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_crew_sos_requests_user_status
        ON crew_sos_requests (user_id, status);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_crew_sos_requests_user_status")
    op.execute("ALTER TABLE notifications DROP COLUMN IF EXISTS sos_id")
    op.execute("DROP TABLE IF EXISTS crew_sos_requests")
