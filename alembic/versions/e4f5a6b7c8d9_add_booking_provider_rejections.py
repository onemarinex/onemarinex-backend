"""Add booking_provider_rejections table

Revision ID: e4f5a6b7c8d9
Revises: d7e8f9a0b1c2
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_provider_rejections",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("aggregator_profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("booking_id", "provider_id", name="uq_booking_provider_rejection"),
    )


def downgrade():
    op.drop_table("booking_provider_rejections")
