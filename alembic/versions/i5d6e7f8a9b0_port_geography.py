"""Add reviewed geography without changing existing port records."""

from alembic import op
import sqlalchemy as sa

revision = "i5d6e7f8a9b0"
down_revision = "h4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            """CREATE TABLE IF NOT EXISTS port_geography (
        port_id INTEGER PRIMARY KEY REFERENCES ports(id),
        country_code VARCHAR(2) NOT NULL,
        evidence_reference VARCHAR(1000) NOT NULL,
        updated_by_user_id INTEGER NOT NULL REFERENCES users(id),
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL
    )"""
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_port_geography_country_code ON port_geography(country_code)"
        )
    )


def downgrade():
    pass
