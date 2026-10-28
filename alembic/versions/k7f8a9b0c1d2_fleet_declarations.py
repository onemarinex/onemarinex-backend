"""Aggregate-only fleet declarations. No legacy row is changed."""

from alembic import op
import sqlalchemy as sa

revision = "k7f8a9b0c1d2"
down_revision = "j6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            """CREATE TABLE IF NOT EXISTS company_fleet_imports (
        id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES shipping_companies(id),
        created_by_user_id INTEGER NOT NULL REFERENCES users(id), filename VARCHAR(255) NOT NULL,
        source_sha256 VARCHAR(64) NOT NULL, columns JSON NOT NULL, rows JSON NOT NULL,
        mapping JSON, result JSON, status VARCHAR(24) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), imported_at TIMESTAMP WITH TIME ZONE
    )"""
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_company_fleet_imports_company_id ON company_fleet_imports(company_id)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS fleet_group VARCHAR(36)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS declared_vessel_name VARCHAR(255)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS fleet_import_id INTEGER REFERENCES company_fleet_imports(id)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_vessel_role_claims_fleet_group ON vessel_role_claims(fleet_group)"
        )
    )
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    op.execute(
        sa.text(
            """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_vessel_role_claims_accepted_no_overlap'
            AND conrelid='vessel_role_claims'::regclass) THEN
        ALTER TABLE vessel_role_claims ADD CONSTRAINT uq_vessel_role_claims_accepted_no_overlap
        EXCLUDE USING gist (vessel_id WITH =, role WITH =,
            tstzrange(effective_start,effective_end,'[)') WITH &&)
        WHERE (status IN ('APPROVED','AUTO_ACCEPTED') AND revoked_at IS NULL);
        END IF; END $$"""
        )
    )


def downgrade():
    # Retain accepted declarations and import audit records.
    pass
