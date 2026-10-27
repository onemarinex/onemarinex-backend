"""Fleet claim submissions and sensitive access requests.

Document 2 section 4's remaining two entities. New tables only: nothing
existing is altered and no row is read or written.

A submission groups the rows of a CSV upload so a rejected one can be explained
without re-reading a file nobody stored. It approves nothing -- section 21:
bulk approval is allowed "only if each row independently passes the same review
and transaction-safe overlap rule".

A request is distinct from the grant it may produce, because section 21 says a
company admin cannot grant itself detail. The request survives the grant's
expiry as the record of why it was asked for.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "z7a8b9c0d1e2"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(inspect(op.get_bind()).get_table_names())

    if "fleet_claim_submissions" not in tables:
        op.create_table(
            "fleet_claim_submissions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("shipping_companies.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("submitted_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source", sa.String(16), nullable=False, server_default="SINGLE"),
            sa.Column("state", sa.String(32), nullable=False, server_default="DRAFT"),
            sa.Column("original_filename", sa.String(255), nullable=True),
            sa.Column("row_count", sa.Integer, nullable=True),
            sa.Column("findings", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_fleet_claim_submissions_company_id",
                        "fleet_claim_submissions", ["company_id"])

    if "sensitive_access_requests" not in tables:
        op.create_table(
            "sensitive_access_requests",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("shipping_companies.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("vessel_call_id", sa.Integer,
                      sa.ForeignKey("vessel_calls.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("category", sa.String(64), nullable=False),
            sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.Text, nullable=False),
            sa.Column("requested_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="REQUESTED"),
            sa.Column("decided_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decision_reason", sa.Text, nullable=True),
            sa.Column("grant_id", sa.Integer,
                      sa.ForeignKey("sensitive_access_grants.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_sensitive_requests_company_id",
                        "sensitive_access_requests", ["company_id"])
        op.create_index("ix_sensitive_requests_queue",
                        "sensitive_access_requests", ["status", "created_at"])


def downgrade():
    # Keep them. A submission's findings explain a rejected upload and a
    # request records why detail was asked for; neither can be reconstructed.
    pass
