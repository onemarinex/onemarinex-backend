"""Add booking_reviews table for driver and facility stop ratings

Revision ID: f1a2b3c4d5e6
Revises: e4f5a6b7c8d9
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("crew_id", sa.Integer(), sa.ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("review_type", sa.String(32), nullable=False, index=True),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("facility_name", sa.String(255), nullable=True),
        sa.Column("facility_stop_id", sa.String(64), nullable=True),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column("review_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("booking_id", "crew_id", "review_type", "driver_id", name="uq_booking_driver_review"),
        sa.UniqueConstraint("booking_id", "crew_id", "review_type", "facility_stop_id", name="uq_booking_facility_review"),
    )


def downgrade():
    op.drop_table("booking_reviews")
