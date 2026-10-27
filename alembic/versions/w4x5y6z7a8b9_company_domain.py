"""The shipping company domain — the four entities the access policy reads.

Document 2 sections 4, 6 and 7. New tables only: nothing existing is altered
and no row is read or written, so deploying this cannot rewrite an existing
database.

On the deployed environment `create_all()` builds the tables at startup, which
is the one thing it does reliably, and `app.main.ensure_company_domain_schema`
adds the exclusion constraint, which create_all cannot express. This migration
is the record of the change and the path for any environment that does run
Alembic; the two are written to match.

The overlap constraint is the point of section 6: the same role on the same
vessel must not be held over overlapping periods, enforced by the database
because two simultaneous approvals must not both succeed. Half-open tstzrange,
so a handover at the boundary instant is not an overlap, and scoped to live
approved rows so a pending or revoked claim cannot block a real one.

The role taxonomy is still open (owner question Q5), so `role` is a string
rather than an enum: agreeing the values later needs no schema change, and the
constraint is keyed on whatever is stored either way.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "w4x5y6z7a8b9"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def _timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def upgrade():
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())

    if "shipping_companies" not in tables:
        op.create_table(
            "shipping_companies",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("legal_name", sa.String(255), nullable=False),
            sa.Column("normalized_name", sa.String(255), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verified_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("normalized_name",
                                name="uq_shipping_companies_normalized_name"),
        )
        op.create_index("ix_shipping_companies_normalized_name",
                        "shipping_companies", ["normalized_name"])

    if "company_user_memberships" not in tables:
        op.create_table(
            "company_user_memberships",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("shipping_companies.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role", sa.String(32), nullable=False, server_default="VIEWER"),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("invited_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("company_id", "user_id",
                                name="uq_company_membership_user"),
        )

    if "vessel_role_claims" not in tables:
        op.create_table(
            "vessel_role_claims",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("shipping_companies.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("vessel_id", sa.Integer,
                      sa.ForeignKey("vessels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("imo_number", sa.String(100), nullable=True),
            sa.Column("role", sa.String(64), nullable=False),
            sa.Column("effective_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="SUBMITTED"),
            sa.Column("evidence_type", sa.String(120), nullable=True),
            sa.Column("evidence_reference", sa.String(255), nullable=True),
            sa.Column("review_reason", sa.Text, nullable=True),
            sa.Column("approved_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_vessel_role_claims_lookup", "vessel_role_claims",
                        ["vessel_id", "role", "status"])
        op.create_index("ix_vessel_role_claims_company_vessel", "vessel_role_claims",
                        ["company_id", "vessel_id"])

    if "sensitive_access_grants" not in tables:
        op.create_table(
            "sensitive_access_grants",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("shipping_companies.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("vessel_call_id", sa.Integer,
                      sa.ForeignKey("vessel_calls.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("category", sa.String(64), nullable=False),
            sa.Column("effective_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("granted_by_user_id", sa.Integer,
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reason", sa.Text, nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
        )
        op.create_index("ix_sensitive_grants_lookup", "sensitive_access_grants",
                        ["company_id", "vessel_call_id", "category"])

    constraint = bind.execute(sa.text(
        "SELECT 1 FROM pg_constraint WHERE conname = 'uq_vessel_role_claims_no_overlap'"
    )).first()
    if not constraint:
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute("""
            ALTER TABLE vessel_role_claims
            ADD CONSTRAINT uq_vessel_role_claims_no_overlap
            EXCLUDE USING gist (
                vessel_id WITH =,
                role WITH =,
                tstzrange(effective_start, effective_end, '[)') WITH &&
            ) WHERE (status = 'APPROVED' AND revoked_at IS NULL)
        """)


def downgrade():
    # Keep the tables. They hold Super Admin verification decisions and the
    # approved tenure those decisions produced, which cannot be reconstructed
    # from anywhere else, and an unused table costs nothing to leave in place.
    pass
