"""Add company profiles and onboarding drafts. Schema only."""

from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None
DDL = (
    """
CREATE TABLE IF NOT EXISTS company_profiles (
	company_id INTEGER NOT NULL,
	corporate_details JSON NOT NULL,
	logo_png BYTEA,
	updated_by_user_id INTEGER,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (company_id),
	FOREIGN KEY(company_id) REFERENCES shipping_companies (id),
	FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS company_onboarding_drafts (
	id SERIAL NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	company_id INTEGER,
	created_by_user_id INTEGER NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(company_id) REFERENCES shipping_companies (id),
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)

""",
)


def upgrade():
    for sql in DDL:
        op.execute(sa.text(sql))


def downgrade():
    pass  # Retain tenant and audit history.
