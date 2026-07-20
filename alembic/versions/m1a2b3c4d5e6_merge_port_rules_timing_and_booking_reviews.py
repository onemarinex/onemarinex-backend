"""Merge port_rules timing and booking_reviews branches

Revision ID: m1a2b3c4d5e6
Revises: a1b2c3d4e5f6, f1a2b3c4d5e6
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa


revision = "m1a2b3c4d5e6"
down_revision = ("b2c3d4e5f6a7", "f1a2b3c4d5e6")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
