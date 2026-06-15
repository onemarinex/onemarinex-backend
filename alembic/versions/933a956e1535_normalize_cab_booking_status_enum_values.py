"""normalize cab booking status enum values

Revision ID: 933a956e1535
Revises: a1b2c3d4e5f6
Create Date: 2026-06-15 10:15:01.967007

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '933a956e1535'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    lower_status_values = [
        "pending_provider_response",
        "provider_accepted",
        "provider_rejected",
        "driver_assigned",
        "driver_accepted",
        "on_trip",
        "completed",
        "cancelled",
        "pending",
        "confirmed",
        "arrived",
        "in_progress",
    ]

    # Ensure the enum behind cab_bookings.status supports lowercase values used by the app.
    with op.get_context().autocommit_block():
        for status_value in lower_status_values:
            op.execute(
                f"""
                DO $$
                DECLARE enum_type_name text;
                BEGIN
                    SELECT t.typname
                    INTO enum_type_name
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    JOIN pg_type t ON t.oid = a.atttypid
                    WHERE c.relname = 'cab_bookings'
                      AND a.attname = 'status'
                      AND c.relkind = 'r'
                    LIMIT 1;

                    IF enum_type_name IS NOT NULL THEN
                        EXECUTE format(
                            'ALTER TYPE %I ADD VALUE IF NOT EXISTS %L',
                            enum_type_name,
                            '{status_value}'
                        );
                    END IF;
                END $$;
                """
            )

    # Normalize historical uppercase values to lowercase labels.
    legacy_to_lower = {
        "PENDING_PROVIDER_RESPONSE": "pending_provider_response",
        "PROVIDER_ACCEPTED": "provider_accepted",
        "PROVIDER_REJECTED": "provider_rejected",
        "DRIVER_ASSIGNED": "driver_assigned",
        "DRIVER_ACCEPTED": "driver_accepted",
        "ON_TRIP": "on_trip",
        "COMPLETED": "completed",
        "CANCELLED": "cancelled",
        "PENDING": "pending",
        "CONFIRMED": "confirmed",
        "ARRIVED": "arrived",
        "IN_PROGRESS": "in_progress",
    }
    for legacy_status, normalized_status in legacy_to_lower.items():
        op.execute(
            f"""
            UPDATE cab_bookings
            SET status = '{normalized_status}'
            WHERE status::text = '{legacy_status}'
            """
        )


def downgrade() -> None:
    # Enum value removal is not safe in Postgres; keep as a no-op downgrade.
    pass
