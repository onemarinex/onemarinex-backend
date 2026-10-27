"""Give three guard-only schema changes a migration.

Alembic does not run on the deployed environment, so every schema change in
this codebase ships twice: as a migration, and as an `ensure_*` guard in
`app/main.py` that applies the same DDL at startup. The guard is what actually
runs today; the migration is what a database built from scratch would use.

Three changes shipped with a guard and no migration:

    crew_assignments.source                      (HPID spec section 11)
    crew_assignments.signed_on_at / signed_off_at (HPID spec section 8)
    shore_passes.crew_profile_id DROP NOT NULL   (audit R10, owner Q1)

So `alembic upgrade head` against an empty database produced a schema the
application could not run on — the ORM declares all four, and `source` is NOT
NULL. That does not matter while startup guards are the mechanism and the
production database already has them, but it blocks the disposable staging
environment that Gate G1 requires, which is built from migrations alone.

Written to match the guards exactly, and guarded in the same way, so applying
either route to the same database is a no-op for the other.

Additive and reversible-by-omission:

  - `source` is NOT NULL with a server default of MANIFEST, which every
    existing row was, so Postgres fills it without a table rewrite and
    **no row is read or rewritten**;
  - the two movement timestamps are nullable, and null is exactly true of
    every row written before an agent could confirm a movement;
  - dropping NOT NULL only widens what the column accepts, so no existing
    row can violate it.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    assignment_columns = {row["name"] for row in inspector.get_columns("crew_assignments")}
    if "source" not in assignment_columns:
        op.add_column(
            "crew_assignments",
            sa.Column("source", sa.String(32), nullable=False, server_default="MANIFEST"),
        )
    indexes = {row["name"] for row in inspector.get_indexes("crew_assignments")}
    if "ix_crew_assignments_source" not in indexes:
        op.create_index("ix_crew_assignments_source", "crew_assignments", ["source"])

    # Written out rather than looped: tests/test_guard_migration_parity reads
    # this file for the columns it adds, and a name held in a variable is
    # invisible to it. Same reason ensure_ride_otp_attempt_columns is literal.
    if "signed_on_at" not in assignment_columns:
        op.add_column(
            "crew_assignments",
            sa.Column("signed_on_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "signed_off_at" not in assignment_columns:
        op.add_column(
            "crew_assignments",
            sa.Column("signed_off_at", sa.DateTime(timezone=True), nullable=True),
        )

    pass_columns = {row["name"]: row for row in inspector.get_columns("shore_passes")}
    profile_column = pass_columns.get("crew_profile_id")
    if profile_column is not None and not profile_column.get("nullable"):
        # A pass is held against the assignment; the account link may arrive
        # later or never. While this was NOT NULL, no pass could be issued for
        # a crew member without a HeyPorts account at all.
        op.alter_column(
            "shore_passes", "crew_profile_id",
            existing_type=sa.Integer(), nullable=True,
        )


def downgrade():
    # Deliberately empty, as with every migration in this release.
    #
    # Restoring NOT NULL on shore_passes.crew_profile_id would fail against any
    # pass issued to someone without an account -- the exact case this exists to
    # allow -- and dropping `source` would destroy the only record of how an
    # assignment came to be. Rollback here is restore-from-backup; see
    # docs/MIGRATION-ROLLBACK.md.
    pass
