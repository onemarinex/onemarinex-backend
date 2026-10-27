"""Count failed ride-OTP attempts on the booking, not just on the magic link.

`CrewProfile.ride_otp` is four digits and its own column comment calls it a
lifetime OTP: it is not rotated per ride. Two routes check it.

`POST /api/v1/bookings/magic/{token}/verify-otp` has a cooldown and a
five-failure lockout, persisted on the DriverMagicLink row.

`POST /api/v1/drivers/rides/{ride_id}/start` — the authenticated driver app —
had neither. The assigned driver could try all ten thousand values, and a
landed guess is not worth one ride: it is that crew member's OTP from then on,
on every future trip.

These three columns give the booking the same counters the magic link already
had, so one shared policy covers both routes.

Schema only. All three are additive; two are nullable and the counter defaults
to 0 server-side, so **no existing row is read or rewritten**. A booking that
predates them starts from zero attempts, which is true of every one of them.

`app.main.ensure_ride_otp_attempt_columns` applies the same DDL at startup,
because Alembic does not run on the deployed environment. The two are written
to match.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "b8c9d0e1f2a3"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {row["name"] for row in inspect(bind).get_columns("cab_bookings")}

    # Written out rather than looped over a tuple of names:
    # tests/test_guard_migration_parity reads this file to check that every
    # column a startup guard adds also has a migration, and a name held in a
    # variable is invisible to it.
    if "otp_failed_attempts" not in existing:
        op.add_column(
            "cab_bookings",
            sa.Column("otp_failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        )
    if "otp_last_attempt_at" not in existing:
        op.add_column(
            "cab_bookings",
            sa.Column("otp_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "otp_locked_until" not in existing:
        op.add_column(
            "cab_bookings",
            sa.Column("otp_locked_until", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    # Dropping these loses an in-progress lockout, which is the one state that
    # must not be cleared by a rollback.
    pass
