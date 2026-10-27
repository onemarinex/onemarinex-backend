"""The backfill is a deliberate write, so its safety properties are pinned.

Migration u2v3w4x5y6z7 stops at the schema. This script is the half that writes
rows, split out because deploying must not rewrite an existing database. What
matters is that it never overwrites a reference someone may already hold, that
it does nothing at all unless asked, and that it survives two runs racing.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
from unittest import mock

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.session import engine
from app.services.assignment_ref import looks_like_assignment_ref
from scripts import backfill_assignment_public_ref as backfill


def _uniq(prefix):
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        vessel = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(vessel)
        self.db.flush()
        call = VesselCall(vessel_id=vessel.id, vessel_name=vessel.name)
        self.db.add(call)
        self.db.flush()
        self.call_id = call.id

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def _assignment(self, public_ref=None):
        row = CrewAssignment(
            vessel_call_id=self.call_id, crew_name="Crew", rank="AB",
            public_ref=public_ref,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _ref_of(self, assignment_id):
        return self.db.execute(text(
            "SELECT public_ref FROM crew_assignments WHERE id = :id"
        ), {"id": assignment_id}).scalar_one()

    # --- the safety property that matters most -------------------------------

    def test_a_row_that_already_has_a_reference_is_never_rewritten(self):
        """Re-issuing would break a link already handed to a driver."""
        existing = "HP-" + "A" * 26
        row = self._assignment(public_ref=existing)

        self.assertFalse(backfill._issue(self.db, row.id))
        self.assertEqual(self._ref_of(row.id), existing)

    def test_a_row_without_one_is_given_a_well_formed_reference(self):
        row = self._assignment()

        self.assertTrue(backfill._issue(self.db, row.id))
        issued = self._ref_of(row.id)
        self.assertTrue(looks_like_assignment_ref(issued))

    def test_nothing_but_public_ref_is_touched(self):
        row = self._assignment()
        before = self.db.execute(text(
            "SELECT crew_name, rank, hpid, vessel_call_id, started_at "
            "FROM crew_assignments WHERE id = :id"
        ), {"id": row.id}).one()

        backfill._issue(self.db, row.id)

        after = self.db.execute(text(
            "SELECT crew_name, rank, hpid, vessel_call_id, started_at "
            "FROM crew_assignments WHERE id = :id"
        ), {"id": row.id}).one()
        self.assertEqual(tuple(before), tuple(after))

    # --- two runs against one database ---------------------------------------

    def test_a_value_taken_between_generating_and_writing_is_retried(self):
        """The unique index is the authority, not the pre-check."""
        taken = "HP-" + "B" * 26
        self._assignment(public_ref=taken)
        row = self._assignment()

        with mock.patch.object(
            backfill, "generate_assignment_ref",
            side_effect=[taken, "HP-" + "C" * 26],
        ):
            self.assertTrue(backfill._issue(self.db, row.id))

        self.assertEqual(self._ref_of(row.id), "HP-" + "C" * 26)

    def test_it_gives_up_rather_than_spinning_on_a_broken_generator(self):
        taken = "HP-" + "D" * 26
        self._assignment(public_ref=taken)
        row = self._assignment()

        with mock.patch.object(
            backfill, "generate_assignment_ref", return_value=taken,
        ):
            with self.assertRaises(RuntimeError):
                backfill._issue(self.db, row.id)

    # --- counting and the default ---------------------------------------------

    def test_counts_report_what_is_missing(self):
        self._assignment()
        self._assignment()
        self._assignment(public_ref="HP-" + "E" * 26)
        self.db.flush()

        total, missing = backfill._counts(self.db)
        self.assertGreaterEqual(total, 3)
        self.assertGreaterEqual(missing, 2)

    def test_the_default_run_writes_nothing(self):
        row = self._assignment()
        self.db.flush()

        with mock.patch.object(backfill, "SessionLocal", return_value=self.db), \
             mock.patch.object(self.db, "commit") as committed:
            self.assertEqual(backfill.main([]), 0)

        committed.assert_not_called()
        self.assertIsNone(self._ref_of(row.id))

    def test_a_nonsense_batch_or_limit_is_refused(self):
        for argv in (["--apply", "--batch", "0"], ["--apply", "--limit", "0"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    backfill.main(argv)


if __name__ == "__main__":
    unittest.main()
