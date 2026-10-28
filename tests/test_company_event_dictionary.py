"""Which timestamp is authoritative per event family, and how good it is.

Document 2 section 12 requires the dictionary be frozen "after source schema
inspection" and that "any missing/ambiguous source timestamp results in
explicit data quality status; do not silently assume".

The inspection found that **no table in this database records when an event
occurred, only when it was recorded**. These pin the consequences of that,
because the tempting fix -- quietly using created_at everywhere -- puts events
on the wrong company's dashboard at a handover.
"""

import unittest

from sqlalchemy import text

from app.db.session import engine
from app.services.company_event_dictionary import (
    EVENT_DICTIONARY,
    Quality,
    UnknownEventFamily,
    event_time,
    families_reporting_rather_than_occurrence,
    families_with_no_usable_time,
)


class DictionaryShapeTests(unittest.TestCase):
    def test_every_family_states_a_quality_and_a_caveat(self):
        for name, entry in EVENT_DICTIONARY.items():
            with self.subTest(family=name):
                self.assertIn(
                    entry.quality,
                    (
                        Quality.AUTHORITATIVE,
                        Quality.RECORDED_NOT_OCCURRED,
                        Quality.PLANNED,
                        Quality.UNAVAILABLE,
                    ),
                )
                self.assertTrue(entry.caveat.strip(), "a caveat is not optional")

    def test_an_unavailable_family_names_no_column(self):
        """Naming one would invite someone to use it."""
        for name, entry in EVENT_DICTIONARY.items():
            if entry.quality == Quality.UNAVAILABLE:
                with self.subTest(family=name):
                    self.assertIsNone(entry.column)

    def test_an_unlisted_family_raises_rather_than_defaulting(self):
        with self.assertRaises(UnknownEventFamily) as caught:
            event_time("something_new")
        self.assertIn("do not fall back to created_at", str(caught.exception))


class AgainstTheRealSchemaTests(unittest.TestCase):
    """Every column the dictionary names must exist. The document requires the
    dictionary be frozen after inspection; this keeps it honest afterwards."""

    def _columns(self, table):
        with engine.connect() as c:
            return {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"), {"t": table})}

    def test_every_named_column_exists(self):
        for name, entry in EVENT_DICTIONARY.items():
            if entry.table is None or entry.column is None:
                continue
            with self.subTest(family=name):
                self.assertIn(entry.column, self._columns(entry.table))

    def test_incidents_is_still_the_only_table_recording_an_occurrence(self):
        """This began as a canary for "no table records when anything happened",
        and it fired: `incidents.occurred_at` was added on 26 Oct 2026. It now
        guards the next step -- if another table gains one, that family should
        be upgraded from RECORDED_NOT_OCCURRED rather than left as it is.

        shore_pass_exit and shore_pass_return are the two waiting for it."""
        with engine.connect() as c:
            found = sorted(r[0] for r in c.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name LIKE '%occur%'")))
        self.assertEqual(
            found,
            ["crew_movement_events", "incidents"],
            f"occurrence columns changed: {found} — review the dictionary",
        )

    def test_the_shadow_trip_columns_are_still_unused(self):
        """cab_bookings has both trip_started_at and started_at. The dictionary
        picks the populated one; this catches that changing."""
        with engine.connect() as c:
            row = c.execute(text(
                "SELECT count(started_at) a, count(completed_at) b FROM cab_bookings"
            )).one()
        self.assertEqual((row.a, row.b), (0, 0))


class ConsequenceTests(unittest.TestCase):
    def test_the_two_families_this_schema_cannot_attribute(self):
        self.assertEqual(
            families_with_no_usable_time(),
            ("shore_pass_approved",),
        )

    def test_shore_pass_approval_is_unavailable_not_approximated(self):
        entry = event_time("shore_pass_approved")
        self.assertEqual(entry.quality, Quality.UNAVAILABLE)
        self.assertNotEqual(entry.column, "created_at")

    def test_an_incident_can_now_carry_a_real_occurrence(self):
        entry = event_time("incident_occurred")
        self.assertEqual(entry.quality, Quality.AUTHORITATIVE)
        self.assertEqual(entry.column, "occurred_at")

    def test_a_row_without_one_still_falls_back_to_the_report_time(self):
        """Nullable on purpose: existing rows were not rewritten, and a
        reporter who does not know is not pushed into inventing one."""
        entry = event_time("incident_reported")
        self.assertEqual(entry.quality, Quality.RECORDED_NOT_OCCURRED)
        self.assertEqual(entry.column, "created_at")

    def test_an_sos_is_authoritative_because_raising_it_is_the_event(self):
        self.assertEqual(event_time("sos_raised").quality, Quality.AUTHORITATIVE)

    def test_the_families_that_misattribute_across_a_handover_are_named(self):
        self.assertEqual(
            families_reporting_rather_than_occurrence(),
            ("incident_reported", "shore_pass_exit", "shore_pass_return"),
        )


if __name__ == "__main__":
    unittest.main()
