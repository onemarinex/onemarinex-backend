"""Add reviewed company fleet change requests. No existing data is changed."""

from alembic import op
import sqlalchemy as sa

revision = "j6e7f8a9b0c1"
down_revision = "i5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            """CREATE TABLE IF NOT EXISTS company_fleet_changes (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES shipping_companies(id),
        claim_id INTEGER NOT NULL REFERENCES vessel_role_claims(id),
        requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
        action VARCHAR(16) NOT NULL, payload JSON NOT NULL, reason TEXT NOT NULL,
        status VARCHAR(32) NOT NULL,
        decided_by_user_id INTEGER REFERENCES users(id), decision_reason TEXT,
        replacement_claim_id INTEGER REFERENCES vessel_role_claims(id),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, decided_at TIMESTAMP WITH TIME ZONE
    )"""
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_company_fleet_changes_company_id ON company_fleet_changes(company_id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_company_fleet_changes_claim_id ON company_fleet_changes(claim_id)"
        )
    )


def downgrade():
    pass
