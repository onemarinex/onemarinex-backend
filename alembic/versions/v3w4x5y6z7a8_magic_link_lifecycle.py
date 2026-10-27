"""Give driver trip links an expiry and a revocation.

Audit R03: the public GET resolved a token and served the trip payload with no
expiry, no revocation and no driver binding, so a holder of an old URL kept
reading it for as long as the row existed.

Schema only. No row is read and no row is written -- deploying this must not
rewrite an existing database, and it does not need to: a null `expires_at` is
read as creation plus the configured TTL, so the policy reaches links created
before the column existed without touching one of them.

`app.main.ensure_magic_link_lifecycle()` applies the same DDL at startup,
because Alembic does not run on the deployed environment. The two are written
to match.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "v3w4x5y6z7a8"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row["name"] for row in inspect(bind).get_columns("driver_magic_links")}
    if "expires_at" not in columns:
        op.add_column(
            "driver_magic_links",
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "revoked_at" not in columns:
        op.add_column(
            "driver_magic_links",
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    # Keep both columns. Dropping them would make every link immortal again,
    # which is the condition the audit found, and an unused nullable column
    # costs nothing to leave in place.
    pass
