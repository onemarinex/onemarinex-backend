"""Add explicit disclosures and immutable company reports. Schema only."""

from alembic import op
import sqlalchemy as sa

revision = "h4c5d6e7f8a9"
down_revision = "g3b4c5d6e7f8"
branch_labels = None
depends_on = None
DDL = (
    """
CREATE TABLE IF NOT EXISTS company_safety_escalations (
	id SERIAL NOT NULL,
	company_id INTEGER NOT NULL,
	vessel_call_id INTEGER NOT NULL,
	source_kind VARCHAR(16) NOT NULL,
	source_id INTEGER NOT NULL,
	summary TEXT NOT NULL,
	urgency VARCHAR(32) NOT NULL,
	created_by_user_id INTEGER NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_company_safety_recipient_source UNIQUE (company_id, source_kind, source_id),
	FOREIGN KEY(company_id) REFERENCES shipping_companies (id),
	FOREIGN KEY(vessel_call_id) REFERENCES vessel_calls (id),
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_company_safety_escalations_company_id ON company_safety_escalations (company_id)""",
    """CREATE INDEX IF NOT EXISTS ix_company_safety_escalations_vessel_call_id ON company_safety_escalations (vessel_call_id)""",
    """
CREATE TABLE IF NOT EXISTS company_disclosure_requests (
	id SERIAL NOT NULL,
	company_id INTEGER NOT NULL,
	source_kind VARCHAR(16) NOT NULL,
	source_id INTEGER NOT NULL,
	vessel_call_id INTEGER NOT NULL,
	requested_by_user_id INTEGER NOT NULL,
	fields JSON NOT NULL,
	reason TEXT NOT NULL,
	event_start TIMESTAMP WITH TIME ZONE NOT NULL,
	event_end TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(32) NOT NULL,
	approved_scope JSON,
	access_expires_at TIMESTAMP WITH TIME ZONE,
	decision_reason TEXT,
	decided_by_user_id INTEGER,
	revoked_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(company_id) REFERENCES shipping_companies (id),
	FOREIGN KEY(vessel_call_id) REFERENCES vessel_calls (id),
	FOREIGN KEY(requested_by_user_id) REFERENCES users (id),
	FOREIGN KEY(decided_by_user_id) REFERENCES users (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_company_disclosure_requests_company_id ON company_disclosure_requests (company_id)""",
    """CREATE INDEX IF NOT EXISTS ix_company_disclosure_requests_vessel_call_id ON company_disclosure_requests (vessel_call_id)""",
    """
CREATE TABLE IF NOT EXISTS company_management_reports (
	id SERIAL NOT NULL,
	company_id INTEGER NOT NULL,
	report_type VARCHAR(32) NOT NULL,
	payload JSON NOT NULL,
	payload_sha256 VARCHAR(64) NOT NULL,
	created_by_user_id INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(company_id) REFERENCES shipping_companies (id),
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_company_management_reports_company_id ON company_management_reports (company_id)""",
)


def upgrade():
    for sql in DDL:
        op.execute(sa.text(sql))


def downgrade():
    pass
