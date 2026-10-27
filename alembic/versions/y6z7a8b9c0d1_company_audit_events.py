"""Append-only audit for every privileged company decision.

Document 2 section 21: "All approval/revocation actions must record Admin user,
timestamp, legal/operational evidence, before/after role interval, reason and
affected vessel/claims."

Append-only is the point rather than a preference. A corrected claim must
preserve who previously held the role, and an in-place edit erases exactly what
a later dispute turns on, so `app/services/company_claims.py` never updates or
deletes a row here.

New table only. Nothing existing is altered and no row is read or written. On
the deployed environment `create_all()` builds it at startup -- creating a
missing table is the one thing it does reliably -- so this is the record of the
change and the path for any environment that runs Alembic.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "y6z7a8b9c0d1"
down_revision = "x5y6z7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "company_audit_events" in set(inspect(bind).get_table_names()):
        return
    op.create_table(
        "company_audit_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_id", sa.Integer,
                  sa.ForeignKey("shipping_companies.id", ondelete="CASCADE"),
                  nullable=True),
        # Strings rather than foreign keys, so an audit row survives the
        # deletion of whatever it describes.
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.Integer, nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("before_state", sa.JSON, nullable=True),
        sa.Column("after_state", sa.JSON, nullable=True),
        sa.Column("evidence_type", sa.String(120), nullable=True),
        sa.Column("evidence_reference", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_company_audit_target", "company_audit_events",
                    ["target_type", "target_id"])
    op.create_index("ix_company_audit_company_time", "company_audit_events",
                    ["company_id", "created_at"])


def downgrade():
    # Keep it. An audit log that can be dropped by a rollback is not one.
    pass
