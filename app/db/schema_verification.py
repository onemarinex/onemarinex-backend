"""Refuse to start rather than start broken.

The startup guards in `app.main` each catch their own exception and log it, so
a guard that fails leaves the application booting normally with a column the
ORM declares and the database does not have. Every query touching that table
then fails, and the deployment reports success throughout.

An app that is down and says so gets rolled back in minutes. An app that is up
and broken gets discovered by a user. DigitalOcean aborts a deployment whose
process exits, and the previous version keeps serving -- which is the outcome
worth having, so this raises.

Only the tables whose columns arrive through a startup guard are checked. A
mismatch there is an outage; drift elsewhere is a job for migrations, and
widening this would turn an unrelated model edit into a failed deploy.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect

from app.db.session import engine

logger = logging.getLogger("app.startup")


# Tables whose columns are added by an `ensure_*` guard rather than by a
# migration, with the columns that must exist for the ORM's own SELECT to
# parse.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "vessel_role_claims": ("fleet_group", "declared_vessel_name", "fleet_import_id"),
    "crew_assignments": ("public_ref", "source"),
    "driver_magic_links": ("expires_at", "revoked_at"),
    "incidents": ("occurred_at",),
    # otp_failed_attempts is NOT NULL and the ORM selects all three, so a
    # failed guard breaks every query against the busiest table in the app.
    # cab_bookings used to sit in DELIBERATELY_UNVERIFIED, and these columns
    # slipped in under that exemption because the coverage test only asked
    # whether the *table* had been considered. It now asks about columns.
    "cab_bookings": ("otp_failed_attempts", "otp_last_attempt_at", "otp_locked_until"),
    # Found by the NOT NULL rule below rather than chosen: this one was
    # exempt and should never have been. If it is ever absent, every INSERT
    # into aggregator_profiles is already failing, so refusing to start is
    # not a new risk -- it is the existing one, reported.
    "aggregator_profiles": ("provider_type",),
    "ports": ("canonical_key",),
    "vendors": ("commission_percentage",),
    # failed_auth_attempts is NOT NULL: a guard that fails to add it takes
    # every sign-in down, which is the whole application.
    "users": ("failed_auth_attempts", "last_auth_attempt_at", "auth_locked_until"),
    "drivers": ("failed_auth_attempts", "last_auth_attempt_at", "auth_locked_until"),
}

# Columns that gain their place at startup and are deliberately NOT verified,
# for one reason: these guards shipped in earlier releases and the running
# application demonstrably selects them today, so they are not a deployment
# risk -- whereas verifying them would block every future deploy if any one
# were absent for a benign reason nobody can check without production access.
#
# Keyed by column, not by table. A table-level exemption is too coarse: a new
# column on an already-exempt table inherits an exemption written about
# different columns, which is exactly how the cab_bookings OTP counters were
# nearly shipped unverified.
DELIBERATELY_UNVERIFIED: dict[str, frozenset[str]] = {
    "agent_profiles": frozenset({"agency_logo_url", "agency_rules", "support_number"}),
    "cab_bookings": frozenset({"vessel_id"}),
    "crew_profiles": frozenset({"selected_vessel_id"}),
    "notifications": frozenset({"audience_type", "target_vessel_ids"}),
    "port_rules": frozenset({"timezone"}),
    "vessels": frozenset({"shore_pass_valid_upto"}),
}


def missing_guarded_columns() -> dict[str, list[str]]:
    """Guarded columns the database does not have. Empty when healthy."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing: dict[str, list[str]] = {}
    for table, required in GUARDED_COLUMNS.items():
        if table not in tables:
            # A table that does not exist at all is create_all's job, and it
            # does that reliably. Not this function's failure to report.
            continue
        present = {column["name"] for column in inspector.get_columns(table)}
        absent = [name for name in required if name not in present]
        if absent:
            missing[table] = absent
    return missing


def verify_critical_schema() -> None:
    """Raise if a startup guard did not apply. Called last, after them all."""
    missing = missing_guarded_columns()
    if not missing:
        logger.info(
            "schema verification passed: %d guarded table(s) complete",
            len(GUARDED_COLUMNS),
        )
        return

    detail = "; ".join(
        f"{table} is missing {', '.join(columns)}"
        for table, columns in sorted(missing.items())
    )
    logger.error("schema verification FAILED: %s", detail)
    raise RuntimeError(
        "Refusing to start: a startup schema guard did not apply, so the "
        f"database is missing columns the application selects. {detail}. "
        "The most likely cause is an ALTER TABLE that could not take its lock "
        "within the timeout -- retrying the deployment when the table is "
        "quieter usually resolves it. Starting anyway would serve a broken "
        "application that reports healthy."
    )
