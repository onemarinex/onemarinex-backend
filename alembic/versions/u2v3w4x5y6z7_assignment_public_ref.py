"""Give every crew assignment an opaque public reference.

The public identifier today is HP-{passport}-{NAT}-{PORT}. Anyone entitled to
see it — a driver holding a trip link, an aggregator, an agent — receives a
passport number with it, and the same person carries the same value from voyage
to voyage, so the identifier itself links a person's port calls together.

This adds the replacement beside it: random, meaningless, and issued per
assignment rather than per person.

Schema only. No row is read and no row is written.

    An earlier version of this migration also backfilled `public_ref` on every
    existing assignment. It destroyed nothing, but it was a write across a live
    table, and the owner's standing instruction is that deploying this must not
    migrate existing production data. The backfill now lives in
    scripts/backfill_assignment_public_ref.py: a dry run by default, bounded,
    resumable, and run deliberately rather than as a side effect of a deploy.

Deliberately additive and deliberately inert. The column is nullable, nothing
requires it, and no legacy hpid is touched. An assignment with a null reference
behaves exactly as it did before — which is also what makes the backfill safe to
defer, to run in slices, or to stop halfway.

The unique index is created here rather than with the backfill, so the database
is the guarantee from the first moment a reference exists. Postgres treats NULLs
as distinct, so the index is satisfied by a table where every row is still null;
verified on PostgreSQL 16 before this was relied on.

The downgrade keeps the column: dropping it would destroy references that may
already have been handed to drivers and cab providers, and re-running the
backfill would mint different ones, so the old values could never be recovered.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row["name"] for row in inspect(bind).get_columns("crew_assignments")}
    if "public_ref" not in columns:
        op.add_column(
            "crew_assignments",
            sa.Column("public_ref", sa.String(length=40), nullable=True),
        )

    indexes = {row["name"] for row in inspect(bind).get_indexes("crew_assignments")}
    if "uq_crew_assignments_public_ref" not in indexes:
        op.create_index(
            "uq_crew_assignments_public_ref",
            "crew_assignments",
            ["public_ref"],
            unique=True,
        )


def downgrade():
    # Keep the column and anything in it. See the module docstring.
    pass
