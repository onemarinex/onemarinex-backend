"""The manifest must apply the same passport rule as registration.

Registration calls `validate_passport_number`, which refuses placeholders,
values too short to be a passport, and values with no digit. The bulk manifest
save only required a non-empty string, so the manifest was the wider door: what
sign-up turned away arrived through an upload instead, and then propagated into
generated HPIDs and into the uniqueness rules that assume a passport identifies
one person.

Two rows in one file carrying the same passport were a related gap. Rows are
resolved against the database before anything is written, so each of them looked
unseen and both were saved.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_vessels import _save_manifest_rows
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services import crew_manifest as cm
from app.services.crew_identity import PLACEHOLDER_PASSPORTS


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(agent)
        self.db.flush()
        self.vessel = Vessel(
            agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def _row(self, name, passport, nationality="INDIAN", rank="AB"):
        return cm.ParsedCrewRow(
            name=name, rank=rank, nationality=nationality, passport_number=passport,
        )

    def _save(self, rows):
        return _save_manifest_rows(self.db, self.vessel, rows, "Visakhapatnam")

    def _manifest_count(self):
        return self.db.query(VesselCrew).filter(
            VesselCrew.vessel_id == self.vessel.id
        ).count()

    # --- the rule registration already applies --------------------------------

    def test_a_real_passport_still_saves(self):
        """The guard must not break ordinary uploads."""
        self.assertEqual(self._save([self._row("Real Crew", "P7654321")]), 1)
        self.assertEqual(self._manifest_count(), 1)

    def test_a_placeholder_is_refused(self):
        placeholder = sorted(PLACEHOLDER_PASSPORTS)[0]
        with self.assertRaises(HTTPException) as caught:
            self._save([self._row("Placeholder Crew", placeholder)])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("Placeholder Crew", caught.exception.detail)
        self.assertEqual(self._manifest_count(), 0)

    def test_a_value_with_no_digit_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._save([self._row("Letters Only", "ABCDEFGH")])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("digit", caught.exception.detail)

    def test_a_value_too_short_to_be_a_passport_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._save([self._row("Short Crew", "A1")])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("short", caught.exception.detail)

    def test_an_empty_passport_is_still_refused(self):
        """The old behaviour, which must survive the change."""
        with self.assertRaises(HTTPException) as caught:
            self._save([self._row("No Passport", "  ")])
        self.assertEqual(caught.exception.status_code, 422)

    def test_nothing_in_a_rejected_file_is_written(self):
        """A good row ahead of a bad one must not be saved on its own."""
        with self.assertRaises(HTTPException):
            self._save([
                self._row("Good Crew", "P1111111"),
                self._row("Bad Crew", "ABCDEFGH"),
            ])
        self.assertEqual(self._manifest_count(), 0)

    # --- two rows, one passport ----------------------------------------------

    def test_the_same_passport_twice_in_one_file_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._save([
                self._row("First Listing", "P2222222"),
                self._row("Second Listing", "P2222222"),
            ])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("more than once", caught.exception.detail)
        self.assertEqual(self._manifest_count(), 0)

    def test_the_duplicate_message_names_the_earlier_row(self):
        with self.assertRaises(HTTPException) as caught:
            self._save([
                self._row("First Listing", "P3333333"),
                self._row("Second Listing", "P3333333"),
            ])
        self.assertIn("First Listing", caught.exception.detail)

    def test_spacing_does_not_disguise_a_duplicate(self):
        """Normalisation happens before the comparison, as it does for matching."""
        with self.assertRaises(HTTPException) as caught:
            self._save([
                self._row("First Listing", "P4444444"),
                self._row("Second Listing", "P 444 4444"),
            ])
        self.assertIn("more than once", caught.exception.detail)

    def test_two_different_passports_are_not_a_duplicate(self):
        self.assertEqual(self._save([
            self._row("Crew One", "P5555555"),
            self._row("Crew Two", "P6666666"),
        ]), 2)
        self.assertEqual(self._manifest_count(), 2)


if __name__ == "__main__":
    unittest.main()
