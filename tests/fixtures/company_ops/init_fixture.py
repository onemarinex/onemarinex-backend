from app.db.base import Base
from app.db.session import engine
from sqlalchemy import text

if (
    engine.url.database != "company_ops_fixture"
    or engine.url.host != "127.0.0.1"
    or engine.url.port != 55957
):
    raise RuntimeError("Only the disposable company operations fixture is allowed")
Base.metadata.create_all(engine)
with engine.begin() as c:
    c.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    c.execute(
        text(
            """DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_vessel_role_claims_no_overlap') THEN ALTER TABLE vessel_role_claims ADD CONSTRAINT uq_vessel_role_claims_no_overlap EXCLUDE USING gist (vessel_id WITH =, role WITH =, tstzrange(effective_start, effective_end, '[)') WITH &&) WHERE (status = 'APPROVED' AND revoked_at IS NULL); END IF; END $$"""
        )
    )
print("Disposable fixture schema ready")
