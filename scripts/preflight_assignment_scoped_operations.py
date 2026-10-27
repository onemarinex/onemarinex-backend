#!/usr/bin/env python3
"""Read-only pre/postflight for assignment-scoped operations migration.

Historical findings that are unrelated to the migration's constraints remain
visible, but do not block the prevention release.  Use ``--strict-historical``
after the separately approved repair release to require those counters to be
zero as well.
"""

import argparse
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.session import engine


# The revision that introduced the assignment-scoped tables, columns and
# constraints this gate verifies. A historical fact, so it is pinned — but it is
# NOT the expected head. The graph head moves with every later migration, and
# pinning it here is what made this script reject an up-to-date database.
ASSIGNMENT_SCOPED_REVISION = "s0t1u2v3w4x5"

# Databases this gate accepts as a starting point, besides the graph head
# itself: the revision above and the one immediately before it.
SUPPORTED_ORIGINS = {"r9s0t1u2v3w4", ASSIGNMENT_SCOPED_REVISION}


def _applied_revisions(script, current):
    """Every revision the database has applied, by walking down from its head.

    Empty for an unstamped database, which the head check reports separately.
    """
    if not current:
        return set()
    return {rev.revision for rev in script.iterate_revisions(current, "base")}

# These records predate Release A and require evidence-backed Release C work.
# They are not referenced by the schema constraints added in this migration.
DEFERRED_HISTORICAL_CHECKS = {
    "invalid_open_calls",
    "sos_snapshot_context_mismatches",
    "duplicate_equivalent_empty_calls",
}


CHECKS = {
    "duplicate_active_profiles": """
        SELECT count(*) FROM (
          SELECT vessel_call_id, crew_profile_id
          FROM crew_assignments
          WHERE ended_at IS NULL AND crew_profile_id IS NOT NULL
          GROUP BY vessel_call_id, crew_profile_id HAVING count(*) > 1
        ) AS duplicate
    """,
    "duplicate_pending_passports": """
        SELECT count(*) FROM (
          SELECT vessel_call_id,
                 upper(replace(trim(passport_number), ' ', ''))
          FROM crew_assignments
          WHERE ended_at IS NULL AND crew_profile_id IS NULL
            AND passport_number IS NOT NULL AND trim(passport_number) <> ''
          GROUP BY vessel_call_id,
                   upper(replace(trim(passport_number), ' ', ''))
          HAVING count(*) > 1
        ) AS duplicate
    """,
    "duplicate_magic_links": """
        SELECT count(*) FROM (
          SELECT booking_id FROM driver_magic_links
          GROUP BY booking_id HAVING count(*) > 1
        ) AS duplicate
    """,
    "invalid_open_calls": """
        SELECT count(*) FROM vessel_calls AS call
        JOIN vessels AS vessel ON vessel.id = call.vessel_id
        WHERE call.ended_at IS NULL
          AND (lower(vessel.status) IN ('archived', 'departed')
               OR vessel.agent_id IS NULL)
    """,
    "assignment_event_mismatches": """
        SELECT count(*) FROM (
          SELECT booking.id
          FROM cab_bookings AS booking
          JOIN crew_assignments AS assignment
            ON assignment.id = booking.crew_assignment_id
          LEFT JOIN vessel_calls AS call
            ON call.id = booking.vessel_call_id
          WHERE assignment.vessel_call_id IS DISTINCT FROM booking.vessel_call_id
             OR assignment.crew_profile_id IS DISTINCT FROM booking.crew_id
             OR call.vessel_id IS DISTINCT FROM booking.vessel_id
             OR call.agency_id IS DISTINCT FROM booking.agency_id
             OR call.port_id IS DISTINCT FROM booking.port_id
          UNION ALL
          SELECT sos.id
          FROM crew_sos_requests AS sos
          JOIN crew_assignments AS assignment
            ON assignment.id = sos.crew_assignment_id
          LEFT JOIN vessel_calls AS call
            ON call.id = sos.vessel_call_id
          WHERE assignment.vessel_call_id IS DISTINCT FROM sos.vessel_call_id
             OR assignment.crew_profile_id IS DISTINCT FROM sos.crew_profile_id
             OR call.vessel_id IS DISTINCT FROM sos.vessel_id
             OR call.agency_id IS DISTINCT FROM sos.agency_id
             OR call.port_id IS DISTINCT FROM sos.port_id
          UNION ALL
          SELECT incident.id
          FROM incidents AS incident
          JOIN crew_assignments AS assignment
            ON assignment.id = incident.crew_assignment_id
          LEFT JOIN vessel_calls AS call
            ON call.id = incident.vessel_call_id
          WHERE assignment.vessel_call_id IS DISTINCT FROM incident.vessel_call_id
             OR assignment.crew_profile_id IS DISTINCT FROM incident.crew_profile_id
             OR call.vessel_id IS DISTINCT FROM incident.vessel_id
             OR call.agency_id IS DISTINCT FROM incident.agency_id
             OR call.port_id IS DISTINCT FROM incident.port_id
        ) AS mismatch
    """,
    "sos_snapshot_context_mismatches": """
        SELECT count(*)
        FROM crew_sos_requests AS sos
        JOIN vessel_calls AS call ON call.id = sos.vessel_call_id
        LEFT JOIN cab_bookings AS booking ON booking.id = sos.cab_booking_id
        WHERE sos.vessel_id IS DISTINCT FROM call.vessel_id
           OR sos.agency_id IS DISTINCT FROM call.agency_id
           OR sos.port_id IS DISTINCT FROM call.port_id
           OR (
             NULLIF(trim(sos.vessel), '') IS NOT NULL
             AND lower(trim(sos.vessel)) <> lower(trim(call.vessel_name))
           )
           OR (
             sos.cab_booking_id IS NOT NULL
             AND (
               booking.vessel_call_id IS DISTINCT FROM sos.vessel_call_id
               OR booking.vessel_id IS DISTINCT FROM sos.vessel_id
               OR booking.agency_id IS DISTINCT FROM sos.agency_id
               OR booking.crew_assignment_id IS DISTINCT FROM sos.crew_assignment_id
               OR booking.booking_id IS DISTINCT FROM sos.trip_id
             )
           )
    """,
    "unresolved_agent_visible_events": """
        SELECT count(*) FROM (
          SELECT id FROM crew_sos_requests
          WHERE vessel_call_id IS NOT NULL AND agency_id IS NOT NULL
            AND lower(COALESCE(context_resolution, 'unresolved')) = 'unresolved'
          UNION ALL
          SELECT id FROM incidents
          WHERE vessel_call_id IS NOT NULL AND agency_id IS NOT NULL
            AND lower(COALESCE(context_resolution, 'unresolved')) = 'unresolved'
        ) AS unresolved
    """,
    "duplicate_equivalent_empty_calls": """
        WITH empty_calls AS (
          SELECT call.*
          FROM vessel_calls AS call
          WHERE (call.eta IS NOT NULL OR call.etd IS NOT NULL)
            AND NOT EXISTS (
              SELECT 1 FROM cab_bookings AS booking
              WHERE booking.vessel_call_id = call.id
            )
            AND NOT EXISTS (
              SELECT 1 FROM crew_sos_requests AS sos
              WHERE sos.vessel_call_id = call.id
            )
            AND NOT EXISTS (
              SELECT 1 FROM incidents AS incident
              WHERE incident.vessel_call_id = call.id
            )
            AND NOT EXISTS (
              SELECT 1 FROM report_snapshots AS report
              WHERE report.vessel_call_id = call.id
            )
        )
        SELECT count(*) FROM (
          SELECT vessel_id, agency_id, port_id, eta, etd,
                 lower(trim(vessel_name)) AS vessel_snapshot,
                 count(*)
          FROM empty_calls
          GROUP BY vessel_id, agency_id, port_id, eta, etd,
                   lower(trim(vessel_name))
          HAVING count(*) > 1
        ) AS duplicate
    """,
}


IDENTITY_CONFLICT_CHECKS = {
    "duplicate_open_identity_conflicts": """
        SELECT count(*) FROM (
          SELECT vessel_id, passport_key, identity_fingerprint
          FROM crew_identity_conflicts
          WHERE status = 'OPEN'
          GROUP BY vessel_id, passport_key, identity_fingerprint
          HAVING count(*) > 1
        ) AS duplicate
    """,
    "invalid_identity_conflict_states": """
        SELECT count(*)
        FROM crew_identity_conflicts AS conflict
        WHERE conflict.version < 1
           OR conflict.status NOT IN ('OPEN', 'RESOLVED')
           OR (
             conflict.status = 'OPEN'
             AND (
               conflict.resolution_action IS NOT NULL
               OR conflict.selected_profile_id IS NOT NULL
               OR conflict.evidence_type IS NOT NULL
               OR conflict.evidence_reference IS NOT NULL
               OR conflict.resolution_reason IS NOT NULL
               OR conflict.resolved_by_user_id IS NOT NULL
               OR conflict.resolved_at IS NOT NULL
             )
           )
           OR (
             conflict.status = 'RESOLVED'
             AND (
               conflict.resolution_action NOT IN (
                 'SELECT_PROFILE', 'LEAVE_PENDING', 'DISMISS'
               )
               OR NULLIF(trim(conflict.evidence_type), '') IS NULL
               OR NULLIF(trim(conflict.resolution_reason), '') IS NULL
               OR conflict.resolved_by_user_id IS NULL
               OR conflict.resolved_at IS NULL
               OR (
                 conflict.resolution_action = 'SELECT_PROFILE'
                 AND (
                   conflict.selected_profile_id IS NULL
                   OR NOT (
                     conflict.candidate_profile_ids::jsonb
                     @> jsonb_build_array(conflict.selected_profile_id)
                   )
                 )
               )
               OR (
                 conflict.resolution_action <> 'SELECT_PROFILE'
                 AND conflict.selected_profile_id IS NOT NULL
               )
             )
           )
    """,
    "resolved_identity_conflicts_without_audit": """
        SELECT count(*)
        FROM crew_identity_conflicts AS conflict
        WHERE conflict.status = 'RESOLVED'
          AND NOT EXISTS (
            SELECT 1
            FROM crew_identity_conflict_audits AS audit
            WHERE audit.conflict_id = conflict.id
              AND audit.action = conflict.resolution_action
          )
    """,
}


def _is_blocking_finding(name: str, *, strict_historical: bool) -> bool:
    return strict_historical or name not in DEFERRED_HISTORICAL_CHECKS


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict-historical",
        action="store_true",
        help="require deferred historical-repair counters to be zero",
    )
    args = parser.parse_args(argv)
    failures = []
    deferred_findings = []
    with engine.connect() as connection:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        script = ScriptDirectory.from_config(config)
        graph_heads = script.get_heads()
        current = list(MigrationContext.configure(connection).get_current_heads())
        applied = _applied_revisions(script, current)
        # The assignment-scoped structures exist once their revision is in the
        # database's history, not only when it is the database's head. Checking
        # the head alone silently skipped every column check below as soon as a
        # later migration landed.
        assignment_scoped_applied = ASSIGNMENT_SCOPED_REVISION in applied
        print("migration_graph_heads:", graph_heads)
        print("database_current:", current)
        print("assignment_scoped_applied:", assignment_scoped_applied)
        if len(graph_heads) != 1:
            failures.append(
                f"migration graph must have exactly one head; found {graph_heads}"
            )
        elif ASSIGNMENT_SCOPED_REVISION not in {
            rev.revision for rev in script.iterate_revisions(graph_heads[0], "base")
        }:
            failures.append(
                f"graph head {graph_heads[0]} does not descend from "
                f"{ASSIGNMENT_SCOPED_REVISION}; this gate does not apply to it"
            )
        if current != graph_heads and not (set(current) & SUPPORTED_ORIGINS):
            failures.append(
                f"database must be at the graph head or a supported origin "
                f"{sorted(SUPPORTED_ORIGINS)}; found {current}"
            )

        for name, query in CHECKS.items():
            value = connection.execute(text(query)).scalar_one()
            print(f"{name}: {value}")
            if value:
                if _is_blocking_finding(
                    name, strict_historical=args.strict_historical
                ):
                    failures.append(name)
                else:
                    deferred_findings.append(name)
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        identity_tables = {
            "crew_identity_conflicts",
            "crew_identity_conflict_audits",
        }
        if identity_tables.issubset(tables):
            identity_columns = {
                row["name"]
                for row in inspector.get_columns("crew_identity_conflicts")
            }
            identity_audit_columns = {
                row["name"]
                for row in inspector.get_columns(
                    "crew_identity_conflict_audits"
                )
            }
            required_identity_columns = {
                "vessel_id",
                "passport_key",
                "identity_fingerprint",
                "candidate_profile_ids",
                "status",
                "version",
                "resolution_action",
                "selected_profile_id",
                "evidence_type",
                "evidence_reference",
                "resolution_reason",
                "resolved_by_user_id",
                "resolved_at",
            }
            required_identity_audit_columns = {
                "conflict_id",
                "action",
                "expected_version",
                "before_state",
                "after_state",
                "evidence_type",
                "reason",
            }
            missing_identity_columns = sorted(
                required_identity_columns - identity_columns
            )
            missing_identity_audit_columns = sorted(
                required_identity_audit_columns - identity_audit_columns
            )
            print("missing_identity_conflict_columns:", missing_identity_columns)
            print(
                "missing_identity_conflict_audit_columns:",
                missing_identity_audit_columns,
            )
            if missing_identity_columns or missing_identity_audit_columns:
                if assignment_scoped_applied:
                    failures.append(
                        "identity conflict queue columns are missing"
                    )
            else:
                for name, query in IDENTITY_CONFLICT_CHECKS.items():
                    value = connection.execute(text(query)).scalar_one()
                    print(f"{name}: {value}")
                    if value:
                        failures.append(name)
        elif assignment_scoped_applied:
            failures.append("identity conflict queue tables are missing")
        columns = {
            row["name"] for row in inspector.get_columns("cab_bookings")
        }
        if "client_idempotency_key" in columns:
            duplicate_keys = connection.execute(text("""
                SELECT count(*) FROM (
                  SELECT crew_id, client_idempotency_key
                  FROM cab_bookings
                  WHERE client_idempotency_key IS NOT NULL
                  GROUP BY crew_id, client_idempotency_key
                  HAVING count(*) > 1
                ) AS duplicate
            """)).scalar_one()
            print("duplicate_booking_idempotency_keys:", duplicate_keys)
            if duplicate_keys:
                failures.append("duplicate_booking_idempotency_keys")
        assignment_columns = {
            row["name"] for row in inspector.get_columns("crew_assignments")
        }
        shore_pass_columns = {
            row["name"] for row in inspector.get_columns("shore_passes")
        }
        sos_note_columns = {
            row["name"] for row in inspector.get_columns("crew_sos_notes")
        }
        indexes = {
            row["name"] for row in inspector.get_indexes("cab_bookings")
        }
        assignment_indexes = {
            row["name"] for row in inspector.get_indexes("crew_assignments")
        }
        link_indexes = {
            row["name"] for row in inspector.get_indexes("driver_magic_links")
        }
        shore_pass_indexes = {
            row["name"] for row in inspector.get_indexes("shore_passes")
        }
        sos_note_indexes = {
            row["name"] for row in inspector.get_indexes("crew_sos_notes")
        }
        identity_indexes = (
            {
                row["name"]
                for row in inspector.get_indexes("crew_identity_conflicts")
            }
            if "crew_identity_conflicts" in tables
            else set()
        )
        identity_audit_indexes = (
            {
                row["name"]
                for row in inspector.get_indexes(
                    "crew_identity_conflict_audits"
                )
            }
            if "crew_identity_conflict_audits" in tables
            else set()
        )
        if assignment_scoped_applied:
            required = {"client_idempotency_key", "request_fingerprint"}
            if not required.issubset(columns):
                failures.append("cab booking idempotency columns are missing")
            if not {"emergency_email", "shore_pass_eligible"}.issubset(
                assignment_columns
            ):
                failures.append("assignment snapshot columns are missing")
            if not {"crew_assignment_id", "vessel_call_id"}.issubset(
                shore_pass_columns
            ):
                failures.append("shore pass assignment context is missing")
            required_sos_note_columns = {
                "author_user_id", "last_edited_by_user_id", "edited_at",
            }
            if not required_sos_note_columns.issubset(sos_note_columns):
                failures.append("SOS note authorship columns are missing")
            for name, existing in (
                ("uq_cab_bookings_crew_idempotency_key", indexes),
                ("uq_crew_assignments_active_profile", assignment_indexes),
                (
                    "uq_crew_assignments_active_pending_passport",
                    assignment_indexes,
                ),
                ("uq_driver_magic_links_booking_id", link_indexes),
                ("ix_shore_passes_crew_assignment_id", shore_pass_indexes),
                ("ix_shore_passes_vessel_call_id", shore_pass_indexes),
                ("ix_crew_sos_notes_author_user_id", sos_note_indexes),
                ("ix_crew_identity_conflicts_queue", identity_indexes),
                ("ix_crew_identity_conflicts_identity", identity_indexes),
                ("uq_crew_identity_conflicts_open_identity", identity_indexes),
                (
                    "ix_crew_identity_conflict_audits_conflict_id",
                    identity_audit_indexes,
                ),
            ):
                if name not in existing:
                    failures.append(f"missing unique index {name}")
    if deferred_findings:
        print("\nDEFERRED HISTORICAL FINDINGS — Release C verification required")
        for finding in deferred_findings:
            print(f"- {finding}")
        print("Run with --strict-historical after approved repairs.")
    if failures:
        print("\nBLOCKED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nREADY — assignment-scoped operations migration preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
