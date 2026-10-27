"""Record when an incident happened, not only when it was reported.

Document 2 section 12 asks for "incident occurrence timestamp if trustworthy".
Schema inspection on 26 October 2026 found there was none: `incidents` carried
created_at, updated_at, resolved_at and cancelled_at, and nothing for when the
incident occurred.

That gap is not cosmetic at a management handover. An incident reported on
2 July about something that happened on 29 June attributes to the incoming
company, because created_at is all there is to attribute by. Section 7 requires
each event to be attributed by its own occurrence timestamp, and this column is
what makes that possible for incidents.

Schema only. No row is read and no row is written. Existing rows keep a null
occurrence deliberately: there is nowhere to recover it from, inventing one
would be the silent assumption section 12 forbids, and the event dictionary
reports those rows as reporting-time rather than pretending otherwise.

`app.main.ensure_incident_occurred_at` applies the same DDL at startup, because
Alembic does not run on the deployed environment. The two are written to match.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "x5y6z7a8b9c0"
down_revision = "w4x5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row["name"] for row in inspect(bind).get_columns("incidents")}
    if "occurred_at" not in columns:
        op.add_column(
            "incidents",
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {row["name"] for row in inspect(bind).get_indexes("incidents")}
    if "ix_incidents_occurred_at" not in indexes:
        op.create_index("ix_incidents_occurred_at", "incidents", ["occurred_at"])


def downgrade():
    # Keep the column. Dropping it would destroy the one timestamp that says
    # when an incident actually happened, and it cannot be recomputed.
    pass
