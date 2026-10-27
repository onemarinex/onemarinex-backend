"""Count failed password attempts on users and drivers.

`POST /auth/login`, `/auth/login-unified` and `/drivers/login` each compared a
password and raised 401. No counter, no cooldown, no lockout — an unlimited
guessing surface against every account in the system, superadmin included.
`login_events` records only successful logins, so nothing recorded a failure
either.

These are the same three counters DriverMagicLink and CabBooking already carry
for OTPs, under names that say what they are for, read by the one shared
policy in `app/services/attempt_limits`.

Schema only, additive. The counter is NOT NULL with a server default of 0,
which Postgres fills without a table rewrite, and the two timestamps are
nullable. **No existing row is read or rewritten** — an account written before
today starts from zero failures, which is true of every one of them.

`app.main.ensure_auth_attempt_columns` applies the same DDL at startup,
because Alembic does not run on the deployed environment. The two are written
to match.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    # Both tables written out in full rather than looped. The table name was a
    # variable in the first version of this file and tests/test_guard_migration
    # _parity could not read it, which is the third time that has happened --
    # the test caught it, which is what it is for.
    inspector = inspect(op.get_bind())

    users = {row["name"] for row in inspector.get_columns("users")}
    if "failed_auth_attempts" not in users:
        op.add_column("users", sa.Column(
            "failed_auth_attempts", sa.Integer(), nullable=False, server_default="0"))
    if "last_auth_attempt_at" not in users:
        op.add_column("users", sa.Column(
            "last_auth_attempt_at", sa.DateTime(timezone=True), nullable=True))
    if "auth_locked_until" not in users:
        op.add_column("users", sa.Column(
            "auth_locked_until", sa.DateTime(timezone=True), nullable=True))

    drivers = {row["name"] for row in inspector.get_columns("drivers")}
    if "failed_auth_attempts" not in drivers:
        op.add_column("drivers", sa.Column(
            "failed_auth_attempts", sa.Integer(), nullable=False, server_default="0"))
    if "last_auth_attempt_at" not in drivers:
        op.add_column("drivers", sa.Column(
            "last_auth_attempt_at", sa.DateTime(timezone=True), nullable=True))
    if "auth_locked_until" not in drivers:
        op.add_column("drivers", sa.Column(
            "auth_locked_until", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    # Dropping these clears an in-progress lockout, which is the one piece of
    # state a rollback must not discard.
    pass
