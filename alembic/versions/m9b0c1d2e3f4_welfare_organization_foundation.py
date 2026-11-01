"""Create isolated welfare organization identity and aggregate scope tables."""
from alembic import op
import sqlalchemy as sa

revision = "m9b0c1d2e3f4"
down_revision = "l8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade():
    # Startup create_all may have created the tables before migrations run.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "welfare_organizations" not in existing:
        op.create_table("welfare_organizations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("legal_name", sa.String(255), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
            sa.Column("verified_at", sa.DateTime(timezone=True)),
            sa.Column("verified_by_user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'REJECTED')", name="ck_welfare_organization_status"),
            sa.CheckConstraint("status != 'ACTIVE' OR (verified_at IS NOT NULL AND verified_by_user_id IS NOT NULL)", name="ck_welfare_active_verified"))
    if "welfare_memberships" not in existing:
        op.create_table("welfare_memberships",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("welfare_organizations.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role", sa.String(32), nullable=False, server_default="VIEWER"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("organization_id", "user_id", name="uq_welfare_membership_user"),
            sa.CheckConstraint("role IN ('ADMIN', 'ANALYST', 'VIEWER')", name="ck_welfare_membership_role"))
        op.create_index("ix_welfare_memberships_organization_id", "welfare_memberships", ["organization_id"])
        op.create_index("ix_welfare_memberships_user_id", "welfare_memberships", ["user_id"])
    if "welfare_scopes" not in existing:
        op.create_table("welfare_scopes",
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("welfare_organizations.id"), primary_key=True),
            sa.Column("countries", sa.JSON(), nullable=False), sa.Column("modules", sa.JSON(), nullable=False),
            sa.Column("data_categories", sa.JSON(), nullable=False), sa.Column("aggregate_levels", sa.JSON(), nullable=False),
            sa.Column("export_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_until", sa.DateTime(timezone=True)),
            sa.CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="ck_welfare_scope_interval"))
    if "welfare_scope_ports" not in existing:
        op.create_table("welfare_scope_ports",
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("welfare_scopes.organization_id"), primary_key=True),
            sa.Column("port_id", sa.Integer(), sa.ForeignKey("ports.id"), primary_key=True))


def downgrade():
    # Preserve organization and membership history.
    pass
