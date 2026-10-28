"""Add staff invitations, exclusive transfers and movement audit. Schema only.

Existing operational rows are untouched. Rollback retains additive tables.
The DDL is frozen here rather than imported from mutable future ORM mappings.
"""

from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None
DDL = (
    """
CREATE TABLE IF NOT EXISTS company_invitations (
	id SERIAL NOT NULL, 
	company_id INTEGER NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	invited_by_user_id INTEGER NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	accepted_at TIMESTAMP WITH TIME ZONE, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES shipping_companies (id), 
	UNIQUE (token_hash), 
	FOREIGN KEY(invited_by_user_id) REFERENCES users (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_company_invitations_company_id ON company_invitations (company_id)""",
    """CREATE INDEX IF NOT EXISTS ix_company_invitations_email ON company_invitations (email)""",
    """
CREATE TABLE IF NOT EXISTS agency_transfers (
	client_request_id VARCHAR(64), 
	request_fingerprint VARCHAR(64), 
	id SERIAL NOT NULL, 
	agency_id INTEGER NOT NULL, 
	vessel_call_id INTEGER NOT NULL, 
	provider_id INTEGER NOT NULL, 
	assignment_ids JSON NOT NULL, 
	movement_type VARCHAR(16) NOT NULL, 
	pickup VARCHAR(500) NOT NULL, 
	destination VARCHAR(500) NOT NULL, 
	travel_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	requirements TEXT, 
	status VARCHAR(32) DEFAULT 'OFFERED' NOT NULL, 
	driver_id INTEGER, 
	driver_token_hash VARCHAR(64), 
	driver_link_expires_at TIMESTAMP WITH TIME ZONE, 
	started_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	created_by_user_id INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agency_transfer_request UNIQUE (agency_id, client_request_id), 
	FOREIGN KEY(agency_id) REFERENCES agent_profiles (id), 
	FOREIGN KEY(vessel_call_id) REFERENCES vessel_calls (id), 
	FOREIGN KEY(provider_id) REFERENCES aggregator_profiles (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id), 
	UNIQUE (driver_token_hash), 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_agency_transfers_agency_id ON agency_transfers (agency_id)""",
    """CREATE INDEX IF NOT EXISTS ix_agency_transfers_provider_id ON agency_transfers (provider_id)""",
    """CREATE INDEX IF NOT EXISTS ix_agency_transfers_vessel_call_id ON agency_transfers (vessel_call_id)""",
    """
CREATE TABLE IF NOT EXISTS agency_transfer_events (
	id SERIAL NOT NULL, 
	transfer_id INTEGER NOT NULL, 
	actor_user_id INTEGER, 
	actor_driver_id INTEGER, 
	action VARCHAR(32) NOT NULL, 
	previous_state JSON, 
	next_state JSON NOT NULL, 
	reason TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(transfer_id) REFERENCES agency_transfers (id), 
	FOREIGN KEY(actor_user_id) REFERENCES users (id), 
	FOREIGN KEY(actor_driver_id) REFERENCES drivers (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_agency_transfer_events_transfer_id ON agency_transfer_events (transfer_id)""",
    """
CREATE TABLE IF NOT EXISTS crew_movement_events (
	id SERIAL NOT NULL, 
	assignment_id INTEGER NOT NULL, 
	actor_user_id INTEGER, 
	action VARCHAR(16) NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(assignment_id) REFERENCES crew_assignments (id), 
	FOREIGN KEY(actor_user_id) REFERENCES users (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_crew_movement_events_assignment_id ON crew_movement_events (assignment_id)""",
)


def upgrade():
    for statement in DDL:
        op.execute(sa.text(statement))


def downgrade():
    pass  # Retain operational and audit history during application rollback.
