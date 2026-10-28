"""Add separate welfare feedback storage. No historical row changes."""

from alembic import op
import sqlalchemy as sa

revision = "g3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None
DDL = (
    """
CREATE TABLE IF NOT EXISTS welfare_feedback (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	assignment_id INTEGER,
	vessel_call_id INTEGER,
	vessel_id INTEGER,
	port_id INTEGER,
	experience_at TIMESTAMP WITH TIME ZONE,
	answers JSON NOT NULL,
	message TEXT NOT NULL,
	contact_opt_in BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_welfare_feedback_user_assignment UNIQUE (user_id, assignment_id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(assignment_id) REFERENCES crew_assignments (id),
	FOREIGN KEY(vessel_call_id) REFERENCES vessel_calls (id),
	FOREIGN KEY(vessel_id) REFERENCES vessels (id),
	FOREIGN KEY(port_id) REFERENCES ports (id)
)

""",
    """CREATE INDEX IF NOT EXISTS ix_welfare_feedback_assignment_id ON welfare_feedback (assignment_id)""",
    """CREATE INDEX IF NOT EXISTS ix_welfare_feedback_experience_at ON welfare_feedback (experience_at)""",
    """CREATE INDEX IF NOT EXISTS ix_welfare_feedback_vessel_id ON welfare_feedback (vessel_id)""",
    """CREATE INDEX IF NOT EXISTS ix_welfare_feedback_port_id ON welfare_feedback (port_id)""",
    """CREATE INDEX IF NOT EXISTS ix_welfare_feedback_user_id ON welfare_feedback (user_id)""",
    """CREATE INDEX IF NOT EXISTS ix_welfare_feedback_vessel_call_id ON welfare_feedback (vessel_call_id)""",
)


def upgrade():
    for sql in DDL:
        op.execute(sa.text(sql))


def downgrade():
    pass
