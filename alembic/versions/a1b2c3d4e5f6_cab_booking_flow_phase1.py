"""cab booking flow phase 1

Revision ID: a1b2c3d4e5f6
Revises: 777c0c7923d9
Create Date: 2026-06-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "777c0c7923d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_foreign_key(inspector, table_name: str, constraint_name: str) -> bool:
    return any(fk["name"] == constraint_name for fk in inspector.get_foreign_keys(table_name))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    new_statuses = [
        "PENDING_PROVIDER_RESPONSE",
        "PROVIDER_ACCEPTED",
        "PROVIDER_REJECTED",
        "DRIVER_ACCEPTED",
        "ON_TRIP",
    ]
    with op.get_context().autocommit_block():
        for status in new_statuses:
            op.execute(f"ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS '{status}'")

    columns = [
        ("ride_type", sa.Column("ride_type", sa.String(length=64), nullable=True)),
        ("vehicle_category", sa.Column("vehicle_category", sa.String(length=64), nullable=True)),
        ("provider_id", sa.Column("provider_id", sa.Integer(), nullable=True)),
        ("provider_response_status", sa.Column("provider_response_status", sa.String(length=32), nullable=True)),
        ("provider_response_at", sa.Column("provider_response_at", sa.DateTime(timezone=True), nullable=True)),
        ("assigned_driver_id", sa.Column("assigned_driver_id", sa.Integer(), nullable=True)),
        ("driver_assigned_at", sa.Column("driver_assigned_at", sa.DateTime(timezone=True), nullable=True)),
        ("driver_accepted_at", sa.Column("driver_accepted_at", sa.DateTime(timezone=True), nullable=True)),
        ("trip_started_at", sa.Column("trip_started_at", sa.DateTime(timezone=True), nullable=True)),
        ("trip_completed_at", sa.Column("trip_completed_at", sa.DateTime(timezone=True), nullable=True)),
        ("helpline_number", sa.Column("helpline_number", sa.String(), nullable=True)),
    ]
    for column_name, column in columns:
        if not _has_column(inspector, "cab_bookings", column_name):
            op.add_column("cab_bookings", column)

    inspector = sa.inspect(bind)
    if not _has_foreign_key(inspector, "cab_bookings", "fk_cab_bookings_provider_id"):
        op.create_foreign_key(
            "fk_cab_bookings_provider_id",
            "cab_bookings",
            "aggregator_profiles",
            ["provider_id"],
            ["id"],
        )
    if not _has_foreign_key(inspector, "cab_bookings", "fk_cab_bookings_assigned_driver_id"):
        op.create_foreign_key(
            "fk_cab_bookings_assigned_driver_id",
            "cab_bookings",
            "drivers",
            ["assigned_driver_id"],
            ["id"],
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("booking_timeline"):
        op.create_table(
            "booking_timeline",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("booking_id", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("event_time", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("actor_type", sa.String(length=32), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["booking_id"], ["cab_bookings.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "booking_timeline", "ix_booking_timeline_booking_id"):
        op.create_index("ix_booking_timeline_booking_id", "booking_timeline", ["booking_id"])
    if not _has_index(inspector, "booking_timeline", "ix_booking_timeline_event_time"):
        op.create_index("ix_booking_timeline_event_time", "booking_timeline", ["event_time"])

    op.execute(
        """
        UPDATE cab_bookings
        SET provider_id = aggregator_id,
            assigned_driver_id = driver_id,
            driver_assigned_at = created_at
        WHERE aggregator_id IS NOT NULL OR driver_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE cab_bookings
        SET status = 'PENDING_PROVIDER_RESPONSE'
        WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_booking_timeline_event_time", table_name="booking_timeline")
    op.drop_index("ix_booking_timeline_booking_id", table_name="booking_timeline")
    op.drop_table("booking_timeline")

    op.drop_constraint("fk_cab_bookings_assigned_driver_id", "cab_bookings", type_="foreignkey")
    op.drop_constraint("fk_cab_bookings_provider_id", "cab_bookings", type_="foreignkey")

    op.drop_column("cab_bookings", "helpline_number")
    op.drop_column("cab_bookings", "trip_completed_at")
    op.drop_column("cab_bookings", "trip_started_at")
    op.drop_column("cab_bookings", "driver_accepted_at")
    op.drop_column("cab_bookings", "driver_assigned_at")
    op.drop_column("cab_bookings", "assigned_driver_id")
    op.drop_column("cab_bookings", "provider_response_at")
    op.drop_column("cab_bookings", "provider_response_status")
    op.drop_column("cab_bookings", "provider_id")
    op.drop_column("cab_bookings", "vehicle_category")
    op.drop_column("cab_bookings", "ride_type")
