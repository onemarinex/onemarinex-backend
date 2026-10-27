"""Crew manifests arrive as CSV, Excel or PDF and must be read before saving.

Upload previously accepted CSV only, and wrote straight to the crew list with no
chance to check what had been read.

CSV and Excel are exercised for real. The PDF path calls Claude, so the client
is stubbed here — the extraction prompt itself cannot be unit tested, but the
plumbing around it can: empty results, unreadable rows, and failures all have to
surface as a message the agent can act on rather than a stack trace.
"""

import csv
import io
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from openpyxl import Workbook

from app.services import crew_manifest as cm


def csv_bytes(rows) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


def xlsx_bytes(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


class CsvManifestTests(unittest.TestCase):
    HEADER = ["Name", "Rank", "Nationality", "Passport Number", "Shore Pass Allowed"]

    def test_reads_crew(self):
        data = csv_bytes([
            self.HEADER,
            ["Malyuga Vitaliy", "MASTER", "UKRAINIAN", "FJ917654", "yes"],
            ["Singh Pooran", "CH ENG", "INDIAN", "ZA182721", "no"],
        ])

        result = cm.parse_manifest(data, "crew.csv")

        self.assertEqual(result.source, "csv")
        self.assertEqual([c.name for c in result.crew], ["Malyuga Vitaliy", "Singh Pooran"])
        self.assertEqual([c.shore_pass_eligible for c in result.crew], [True, False])

    def test_rows_without_a_name_are_skipped_and_reported(self):
        data = csv_bytes([self.HEADER, ["", "", "", "", ""], ["Real Person", "AB", "INDIAN", "X1", ""]])

        result = cm.parse_manifest(data, "crew.csv")

        self.assertEqual(len(result.crew), 1)
        self.assertTrue(any("skipped" in w for w in result.warnings))

    def test_missing_optional_columns_are_warned_not_fatal(self):
        data = csv_bytes([["Name"], ["Solo Crew"]])

        result = cm.parse_manifest(data, "crew.csv")

        self.assertEqual(len(result.crew), 1)
        self.assertIsNone(result.crew[0].rank)
        self.assertTrue(any("rank" in w for w in result.warnings))

    def test_missing_name_column_is_fatal(self):
        data = csv_bytes([["Rank", "Passport"], ["AB", "X1"]])

        with self.assertRaises(cm.ManifestError) as ctx:
            cm.parse_manifest(data, "crew.csv")
        self.assertIn("crew name column", str(ctx.exception))

    def test_various_date_formats(self):
        for text, expected in [
            ("2026-08-20", datetime(2026, 8, 20)),
            ("20/08/2026", datetime(2026, 8, 20)),
            ("20-Aug-2026", datetime(2026, 8, 20)),
        ]:
            data = csv_bytes([["Name", "Valid Upto"], ["Crew", text]])
            result = cm.parse_manifest(data, "crew.csv")
            self.assertEqual(result.crew[0].shore_pass_valid_upto, expected, text)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(cm.ManifestError):
            cm.parse_manifest(b"", "crew.csv")


class ExcelManifestTests(unittest.TestCase):
    def test_reads_crew(self):
        data = xlsx_bytes([
            ["Crew Name", "Rank", "Nationality", "Passport No"],
            ["Naron Jawad", "BOSUN", "INDIAN", "U9082272"],
        ])

        result = cm.parse_manifest(data, "crew.xlsx")

        self.assertEqual(result.source, "excel")
        self.assertEqual(result.crew[0].name, "Naron Jawad")

    def test_header_below_a_title_row_is_found(self):
        """Printed manifests often carry a title and a blank line above the table."""
        data = xlsx_bytes([
            ["CREW LIST - MV TEST"],
            [],
            ["Crew Name", "Rank", "Passport No"],
            ["Ranjan Rohit", "OS", "U3559118"],
        ])

        result = cm.parse_manifest(data, "crew.xlsx")

        self.assertEqual(len(result.crew), 1)
        self.assertEqual(result.crew[0].rank, "OS")

    def test_legacy_xls_is_refused_with_advice(self):
        with self.assertRaises(cm.ManifestError) as ctx:
            cm.parse_manifest(b"anything", "crew.xls")
        self.assertIn(".xlsx", str(ctx.exception))


class PdfManifestTests(unittest.TestCase):
    """The Claude call is stubbed; what is tested is everything around it."""

    def stub(self, parsed_output):
        class _Resp:
            pass
        resp = _Resp()
        resp.parsed_output = parsed_output

        class _Client:
            def __init__(self, **kw):
                self.messages = self
            def parse(self, **kw):
                return resp
        return _Client

    def run_with(self, parsed_output, key="test-key"):
        fake = type("m", (), {"Anthropic": self.stub(parsed_output)})
        with patch.dict("sys.modules", {"anthropic": fake}), \
             patch.object(cm, "ANTHROPIC_API_KEY", key):
            return cm.parse_manifest(b"%PDF-1.4 fake", "crew.pdf")

    def test_reads_crew_and_flags_it_for_checking(self):
        parsed = type("P", (), {"crew": [
            cm.ParsedCrewRow(name="Malyuga Vitaliy", rank="MASTER",
                             nationality="UKRAINIAN", passport_number="FJ917654"),
        ]})()

        result = self.run_with(parsed)

        self.assertEqual(result.source, "pdf")
        self.assertEqual(len(result.crew), 1)
        # A scan can be misread, so the agent is told to check before saving.
        self.assertTrue(any("check the details" in w for w in result.warnings))

    def test_unreadable_rows_are_dropped_and_counted(self):
        parsed = type("P", (), {"crew": [
            cm.ParsedCrewRow(name="Good Crew"),
            cm.ParsedCrewRow(name="   "),
        ]})()

        result = self.run_with(parsed)

        self.assertEqual(len(result.crew), 1)
        self.assertTrue(any("unreadable" in w for w in result.warnings))

    def test_no_crew_found_is_an_actionable_error(self):
        parsed = type("P", (), {"crew": []})()

        with self.assertRaises(cm.ManifestError) as ctx:
            self.run_with(parsed)
        self.assertIn("CSV or Excel", str(ctx.exception))

    def test_missing_api_key_explains_itself(self):
        with patch.object(cm, "ANTHROPIC_API_KEY", ""):
            with self.assertRaises(cm.ManifestError) as ctx:
                cm.parse_manifest(b"%PDF-1.4", "crew.pdf")
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_rejected_credentials_are_reported_as_a_server_problem(self):
        """Not the agent's fault — do not send them chasing a better scan."""
        class _Auth:
            def __init__(self, **kw):
                self.messages = self
            def parse(self, **kw):
                raise RuntimeError("Error code: 401 - authentication_error: API key is invalid.")

        fake = type("m", (), {"Anthropic": _Auth})
        with patch.dict("sys.modules", {"anthropic": fake}), \
             patch.object(cm, "ANTHROPIC_API_KEY", "bad-key"):
            with self.assertRaises(cm.ManifestError) as ctx:
                cm.parse_manifest(b"%PDF-1.4", "crew.pdf")

        message = str(ctx.exception)
        self.assertIn("credentials", message)
        self.assertNotIn("clearer scan", message)

    def test_rate_limits_suggest_retrying(self):
        class _Busy:
            def __init__(self, **kw):
                self.messages = self
            def parse(self, **kw):
                raise RuntimeError("Error code: 429 - rate limit exceeded")

        fake = type("m", (), {"Anthropic": _Busy})
        with patch.dict("sys.modules", {"anthropic": fake}), \
             patch.object(cm, "ANTHROPIC_API_KEY", "k"):
            with self.assertRaises(cm.ManifestError) as ctx:
                cm.parse_manifest(b"%PDF-1.4", "crew.pdf")
        self.assertIn("Try again", str(ctx.exception))

    def test_api_failure_does_not_leak_a_stack_trace(self):
        class _Boom:
            def __init__(self, **kw):
                self.messages = self
            def parse(self, **kw):
                raise RuntimeError("connection reset by peer")

        fake = type("m", (), {"Anthropic": _Boom})
        with patch.dict("sys.modules", {"anthropic": fake}), \
             patch.object(cm, "ANTHROPIC_API_KEY", "bad-key"):
            with self.assertRaises(cm.ManifestError) as ctx:
                cm.parse_manifest(b"%PDF-1.4", "crew.pdf")

        message = str(ctx.exception)
        self.assertIn("Could not read the crew list", message)
        self.assertNotIn("connection reset", message)


class FormatRoutingTests(unittest.TestCase):
    def test_unknown_extension_is_refused(self):
        with self.assertRaises(cm.ManifestError) as ctx:
            cm.parse_manifest(b"data", "crew.docx")
        self.assertIn("CSV, Excel", str(ctx.exception))


class ManifestSaveTests(unittest.TestCase):
    """Saving the reviewed rows, which is where a real manifest was rejected.

    Parsing a manifest and saving it are separate steps, and only parsing was
    covered. Nationalities are validated strictly on save, so a demonym the
    alias table did not know ("UKRAINIAN" — manifests print the demonym far more
    often than the country name) raised on the first offending row and took the
    whole upload down with it. The tests above happily parsed "UKRAINIAN"
    because parsing never normalises, which is why they stayed green while no
    crew list could be imported.

    Runs inside a transaction that is always rolled back.
    """

    def setUp(self):
        import app.db.base  # noqa: F401 — registers every model on Base
        from sqlalchemy.orm import Session
        from app.db.models.user import User
        from app.db.models.vessel import Vessel
        from app.db.session import engine

        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        suffix = uuid.uuid4().hex[:10]
        agent = User(email=f"agent-{suffix}@example.com", hashed_password="x", role="agent")
        self.db.add(agent)
        self.db.flush()
        self.vessel = Vessel(
            agent_id=agent.id, name=f"MV-{suffix}", imo_number=f"IMO-{suffix}",
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def save(self, rows):
        from app.api.v1.routes_vessels import _save_manifest_rows
        return _save_manifest_rows(self.db, self.vessel, rows, "port_test")

    def test_a_manifest_of_demonyms_imports(self):
        rows = [
            cm.ParsedCrewRow(name="Malyuga Vitaliy", rank="MASTER",
                             nationality="UKRAINIAN", passport_number="FJ917654"),
            cm.ParsedCrewRow(name="Kothawale Yogesh Tushar", rank="CH OFF",
                             nationality="INDIAN", passport_number="Z5262WB"),
            cm.ParsedCrewRow(name="Reyes Juan", rank="AB",
                             nationality="FILIPINO", passport_number="P1234567"),
        ]

        self.assertEqual(self.save(rows), 3)

        from app.db.models.vessel_crew import VesselCrew
        saved = {
            row.passport_number: row.nationality
            for row in self.db.query(VesselCrew).filter(
                VesselCrew.vessel_id == self.vessel.id).all()
        }
        self.assertEqual(saved["FJ917654"], "UA")
        self.assertEqual(saved["Z5262WB"], "IN")
        self.assertEqual(saved["P1234567"], "PH")
        self.db.refresh(self.vessel)
        self.assertEqual(self.vessel.crew_count, 3)
        self.assertEqual(self.vessel.total_crew, 3)

    def test_an_unrecognised_nationality_names_the_crew_member_and_the_value(self):
        """The agent has to be told which cell to correct."""
        from fastapi import HTTPException

        # A valid passport, so the row reaches the nationality check this test
        # is about: the manifest now applies registration's passport rule, and
        # "X1" is too short for it.
        rows = [cm.ParsedCrewRow(name="Someone", rank="AB",
                                 nationality="Atlantean", passport_number="X1234567")]

        with self.assertRaises(HTTPException) as ctx:
            self.save(rows)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Someone", ctx.exception.detail)
        self.assertIn("Atlantean", ctx.exception.detail)

    def test_bulk_manifest_is_idempotent_and_scopes_pass_to_assignment(self):
        from app.db.models.crew_assignment import CrewAssignment
        from app.db.models.crew_profile import CrewProfile
        from app.db.models.shore_pass import ShorePass
        from app.db.models.user import User
        from app.db.models.vessel_crew import VesselCrew

        self.vessel.agency_name = "Partner Shipping"
        suffix = uuid.uuid4().hex[:10]
        user = User(
            email=f"crew-{suffix}@example.com",
            hashed_password="x",
            role="crew",
        )
        self.db.add(user)
        self.db.flush()
        profile = CrewProfile(
            user_id=user.id,
            full_name="Verified Crew",
            rank="able_seaman",
            nationality="IN",
            passport_number="IDEMP123",
            hpid=f"HP-STABLE-{suffix}",
        )
        self.db.add(profile)
        self.db.flush()
        rows = [
            cm.ParsedCrewRow(
                name="Verified Crew",
                rank="AB",
                nationality="INDIAN",
                passport_number=" idemp 123 ",
                shore_pass_eligible=True,
            )
        ]

        self.assertEqual(self.save(rows), 1)
        self.assertEqual(self.save(rows), 1)

        manifests = self.db.query(VesselCrew).filter(
            VesselCrew.vessel_id == self.vessel.id
        ).all()
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0].hp_id, profile.hpid)
        self.assertEqual(manifests[0].status, "Mapped")
        assignments = self.db.query(CrewAssignment).filter(
            CrewAssignment.vessel_crew_id == manifests[0].id,
            CrewAssignment.ended_at.is_(None),
        ).all()
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].crew_profile_id, profile.id)
        passes = self.db.query(ShorePass).filter(
            ShorePass.crew_profile_id == profile.id
        ).all()
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0].crew_assignment_id, assignments[0].id)
        self.assertEqual(passes[0].vessel_call_id, assignments[0].vessel_call_id)

    def test_bulk_manifest_refuses_a_passport_shared_by_profiles(self):
        from fastapi import HTTPException
        from app.db.models.crew_profile import CrewProfile
        from app.db.models.user import User
        from app.db.models.vessel_crew import VesselCrew

        suffix = uuid.uuid4().hex[:10]
        users = [
            User(
                email=f"conflict-{index}-{suffix}@example.com",
                hashed_password="x",
                role="crew",
            )
            for index in range(2)
        ]
        self.db.add_all(users)
        self.db.flush()
        self.db.add_all([
            CrewProfile(
                user_id=users[0].id,
                full_name="First Person",
                rank="master",
                nationality="IN",
                passport_number="SHARED999",
                hpid=f"HP-FIRST-{suffix}",
            ),
            CrewProfile(
                user_id=users[1].id,
                full_name="Second Person",
                rank="master",
                nationality="PH",
                passport_number="SHARED999",
                hpid=f"HP-SECOND-{suffix}",
            ),
        ])
        self.db.flush()

        with self.assertRaises(HTTPException) as conflict:
            self.save([
                cm.ParsedCrewRow(
                    name="First Person",
                    rank="MASTER",
                    nationality="INDIAN",
                    passport_number="SHARED999",
                )
            ])

        self.assertEqual(conflict.exception.status_code, 409)
        self.assertIn("identity reconciliation", conflict.exception.detail["message"])
        self.assertEqual(conflict.exception.detail["status"], "OPEN")
        self.assertEqual(
            self.db.query(VesselCrew).filter(
                VesselCrew.vessel_id == self.vessel.id
            ).count(),
            0,
        )

    def test_late_bulk_conflict_does_not_commit_an_earlier_valid_row(self):
        from fastapi import HTTPException
        from app.db.models.crew_identity_conflict import CrewIdentityConflictRecord
        from app.db.models.crew_profile import CrewProfile
        from app.db.models.user import User
        from app.db.models.vessel_crew import VesselCrew

        suffix = uuid.uuid4().hex[:10]
        users = [
            User(
                email=f"late-conflict-{index}-{suffix}@example.com",
                hashed_password="x",
                role="crew",
            )
            for index in range(2)
        ]
        self.db.add_all(users)
        self.db.flush()
        self.db.add_all([
            CrewProfile(
                user_id=users[0].id, full_name="First Candidate", rank="master",
                nationality="IN", passport_number="LATE999",
                hpid=f"HP-LATE-A-{suffix}",
            ),
            CrewProfile(
                user_id=users[1].id, full_name="Second Candidate", rank="master",
                nationality="PH", passport_number="LATE999",
                hpid=f"HP-LATE-B-{suffix}",
            ),
        ])
        self.db.flush()

        with self.assertRaises(HTTPException) as conflict:
            self.save([
                cm.ParsedCrewRow(
                    name="Valid First Row", rank="AB", nationality="INDIAN",
                    passport_number=f"VALID-{suffix}",
                ),
                cm.ParsedCrewRow(
                    name="First Candidate", rank="MASTER", nationality="INDIAN",
                    passport_number="LATE999",
                ),
            ])

        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(
            self.db.query(VesselCrew).filter(
                VesselCrew.vessel_id == self.vessel.id
            ).count(),
            0,
        )
        self.assertEqual(
            self.db.query(CrewIdentityConflictRecord).filter(
                CrewIdentityConflictRecord.id
                == conflict.exception.detail["identity_conflict_id"]
            ).count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
