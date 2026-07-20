"""add booking invitations table

Revision ID: d7e8f9a0b1c2
Revises: c4f1b2d9a7e3
Create Date: 2026-07-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c4f1b2d9a7e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "booking_invitations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invited_crew_id", sa.Integer(), sa.ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invited_by_id", sa.Integer(), sa.ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("booking_id", "invited_crew_id", name="uq_booking_invited_crew"),
    )


def downgrade() -> None:
    op.drop_table("booking_invitations")
