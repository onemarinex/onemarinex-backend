"""Independent fleet identity guards. Existing rows are never backfilled."""

from sqlalchemy import text

IDENTITY_CONSTRAINT = "uq_fleet_identity_role_period"

# This SQL is also frozen in the Alembic revision. Keep both copies equivalent.
DDL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE vessel_role_claims ALTER COLUMN vessel_id DROP NOT NULL;
ALTER TABLE vessel_role_claims ADD COLUMN IF NOT EXISTS fleet_identity_key VARCHAR(120);

CREATE OR REPLACE FUNCTION canonical_fleet_imo(raw TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE n TEXT; checksum INTEGER := 0; i INTEGER;
BEGIN
    n := regexp_replace(upper(btrim(raw)), '^IMO *', '');
    IF n !~ '^[0-9]{7}$' THEN RETURN NULL; END IF;
    FOR i IN 1..6 LOOP
        checksum := checksum + substring(n FROM i FOR 1)::INTEGER * (8-i);
    END LOOP;
    IF checksum % 10 <> substring(n FROM 7 FOR 1)::INTEGER THEN RETURN NULL; END IF;
    RETURN n;
END $$;

CREATE OR REPLACE FUNCTION guard_company_fleet_identity() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE operational_imo TEXT; identity TEXT;
BEGIN
    SELECT canonical_fleet_imo(imo_number) INTO operational_imo
      FROM vessels WHERE id=NEW.vessel_id;
    identity := coalesce(operational_imo, canonical_fleet_imo(NEW.imo_number),
                         'vessel:' || NEW.vessel_id::TEXT);
    IF identity IS NULL THEN
        RAISE EXCEPTION 'An independent fleet declaration requires a valid IMO'
            USING ERRCODE='23514';
    END IF;
    NEW.fleet_identity_key := identity;
    -- Serializes unknown IMOs too. Row locks cannot protect a vessel that does
    -- not exist. Transaction-level locks release on commit and rollback.
    PERFORM pg_advisory_xact_lock(hashtextextended('company-fleet:' || identity, 0));
    IF NEW.status IN ('APPROVED','AUTO_ACCEPTED') AND NEW.revoked_at IS NULL
       AND EXISTS (
        SELECT 1 FROM vessel_role_claims c LEFT JOIN vessels v ON v.id=c.vessel_id
        WHERE c.id IS DISTINCT FROM NEW.id AND c.role=NEW.role
          AND c.status IN ('APPROVED','AUTO_ACCEPTED') AND c.revoked_at IS NULL
          AND (coalesce(canonical_fleet_imo(v.imo_number), c.fleet_identity_key,
                        canonical_fleet_imo(c.imo_number), 'vessel:' || c.vessel_id::TEXT)=identity
               OR (NEW.vessel_id IS NOT NULL AND c.vessel_id=NEW.vessel_id))
          AND tstzrange(c.effective_start,c.effective_end,'[)') &&
              tstzrange(NEW.effective_start,NEW.effective_end,'[)')
       ) THEN
        RAISE EXCEPTION 'Conflicting fleet role period'
            USING ERRCODE='23P01', CONSTRAINT='uq_fleet_identity_role_period';
    END IF;
    RETURN NEW;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgname='company_fleet_identity_guard' AND tgrelid='vessel_role_claims'::regclass) THEN
        CREATE TRIGGER company_fleet_identity_guard BEFORE INSERT OR UPDATE
            ON vessel_role_claims FOR EACH ROW EXECUTE FUNCTION guard_company_fleet_identity();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conname='uq_fleet_identity_role_period' AND conrelid='vessel_role_claims'::regclass) THEN
        ALTER TABLE vessel_role_claims ADD CONSTRAINT uq_fleet_identity_role_period
            EXCLUDE USING gist (fleet_identity_key WITH =, role WITH =,
                tstzrange(effective_start,effective_end,'[)') WITH &&)
            WHERE (status IN ('APPROVED','AUTO_ACCEPTED') AND revoked_at IS NULL);
    END IF;
END $$;
"""


def ensure_fleet_registry_schema(connection):
    connection.execute(text(DDL))
