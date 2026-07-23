"""Add missing port_rules columns: advance_booking_buffer_minutes, contact_email, helpline_number

Revision ID: g9h0i1j2k3l4
Revises: m1a2b3c4d5e6
Create Date: 2026-07-23 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "g9h0i1j2k3l4"
down_revision = "m1a2b3c4d5e6"
branch_labels = None
depends_on = None


def column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return column in [c["name"] for c in insp.get_columns(table)]


def upgrade() -> None:
    if not column_exists("port_rules", "advance_booking_buffer_minutes"):
        op.add_column("port_rules", sa.Column("advance_booking_buffer_minutes", sa.Integer(), server_default="30"))
    if not column_exists("port_rules", "contact_email"):
        op.add_column("port_rules", sa.Column("contact_email", sa.String(length=255), nullable=True))
    if not column_exists("port_rules", "helpline_number"):
        op.add_column("port_rules", sa.Column("helpline_number", sa.String(length=50), nullable=True))


def downgrade() -> None:
    if column_exists("port_rules", "helpline_number"):
        op.drop_column("port_rules", "helpline_number")
    if column_exists("port_rules", "contact_email"):
        op.drop_column("port_rules", "contact_email")
    if column_exists("port_rules", "advance_booking_buffer_minutes"):
        op.drop_column("port_rules", "advance_booking_buffer_minutes")
