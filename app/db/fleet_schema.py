"""Additive fleet schema guard. Never rewrites legacy claims or operational rows."""

from sqlalchemy import text

CONSTRAINT = "uq_vessel_role_claims_accepted_no_overlap"


def ensure_fleet_schema(connection):
    connection.execute(
        text(
            "ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS fleet_group VARCHAR(36)"
        )
    )

    from app.db.fleet_registry_schema import ensure_fleet_registry_schema

    ensure_fleet_registry_schema(connection)
    connection.execute(
        text(
            "ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS declared_vessel_name VARCHAR(255)"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS fleet_import_id INTEGER REFERENCES company_fleet_imports(id)"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_vessel_role_claims_fleet_group ON vessel_role_claims(fleet_group)"
        )
    )
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    connection.execute(
        text(
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


def require_fleet_constraint(db):
    if not db.execute(
        text(
            """SELECT 1 FROM pg_constraint WHERE conname=:name AND conrelid='vessel_role_claims'::regclass
            AND EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_fleet_identity_role_period'
                AND conrelid='vessel_role_claims'::regclass)
            AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='company_fleet_identity_guard'
                AND tgrelid='vessel_role_claims'::regclass AND tgenabled='O')"""
        ),
        {"name": CONSTRAINT},
    ).first():
        from fastapi import HTTPException

        raise HTTPException(
            503,
            "Fleet acceptance is unavailable until its database guard is installed.",
        )
