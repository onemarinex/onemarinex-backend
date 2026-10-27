"""An incident is attributed by when it happened, not when it was reported.

Before `incidents.occurred_at` existed there was only `created_at`, so an
incident reported on 2 July about something on 29 June attributed to whoever
held the vessel in July. At a management handover that is the wrong company,
and Document 2 section 7 requires attribution by the event's own occurrence.

The column is nullable and every row written before it exists carries null, so
the quality of an incident's timestamp is a property of the row. These pin both
halves: a stated occurrence is authoritative, and a row without one falls back
to the report time and is labelled as such rather than passing for the real
thing.
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text

from app.db.session import engine
from app.services.company_event_dictionary import (
    Quality,
    incident_occurrence,
)

OCCURRED = datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc)
REPORTED = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)


class OccurrenceResolutionTests(unittest.TestCase):
    def test_a_stated_occurrence_wins_over_the_report_time(self):
        when, entry = incident_occurrence(
            SimpleNamespace(occurred_at=OCCURRED, created_at=REPORTED)
        )
        self.assertEqual(when, OCCURRED)
        self.assertEqual(entry.quality, Quality.AUTHORITATIVE)

    def test_the_handover_case_lands_on_the_right_side(self):
        """A 1 July handover: the incident belongs to the outgoing company."""
        handover = datetime(2026, 7, 1, tzinfo=timezone.utc)
        when, _ = incident_occurrence(
            SimpleNamespace(occurred_at=OCCURRED, created_at=REPORTED)
        )
        self.assertLess(when, handover)

    def test_without_the_column_it_would_have_landed_on_the_wrong_side(self):
        """What the old behaviour did, kept as the reason this exists."""
        handover = datetime(2026, 7, 1, tzinfo=timezone.utc)
        when, entry = incident_occurrence(
            SimpleNamespace(occurred_at=None, created_at=REPORTED)
        )
        self.assertGreater(when, handover)
        self.assertEqual(entry.quality, Quality.RECORDED_NOT_OCCURRED)

    def test_a_legacy_row_is_labelled_not_silently_equivalent(self):
        _, stated = incident_occurrence(
            SimpleNamespace(occurred_at=OCCURRED, created_at=REPORTED)
        )
        _, legacy = incident_occurrence(
            SimpleNamespace(occurred_at=None, created_at=REPORTED)
        )
        self.assertNotEqual(stated.quality, legacy.quality)

    def test_a_row_with_neither_timestamp_returns_none(self):
        when, _ = incident_occurrence(
            SimpleNamespace(occurred_at=None, created_at=None)
        )
        self.assertIsNone(when)


class ValidationTests(unittest.TestCase):
    def _create(self, **kwargs):
        from app.api.v1.routes_incidents import IncidentCreate
        from app.db.models.incident import IncidentType

        payload = dict(type=IncidentType.CREW, title="t", description="d")
        payload.update(kwargs)
        return IncidentCreate(**payload)

    def test_a_past_occurrence_is_accepted(self):
        moment = datetime.now(timezone.utc) - timedelta(days=3)
        self.assertEqual(self._create(occurred_at=moment).occurred_at, moment)

    def test_omitting_it_is_allowed(self):
        """A reporter who does not know must not be pushed into inventing one."""
        self.assertIsNone(self._create().occurred_at)

    def test_a_future_occurrence_is_refused(self):
        """A mistyped year would park the incident outside every tenure."""
        with self.assertRaises(Exception) as caught:
            self._create(occurred_at=datetime(2036, 1, 1, tzinfo=timezone.utc))
        self.assertIn("future", str(caught.exception).lower())

    def test_a_little_clock_skew_is_tolerated(self):
        """The reporter's clock is a phone at a port, not an atomic standard."""
        self._create(occurred_at=datetime.now(timezone.utc) + timedelta(minutes=5))


class SchemaTests(unittest.TestCase):
    def test_the_column_exists_and_is_indexed(self):
        with engine.connect() as c:
            columns = {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='incidents'"))}
            indexes = {r[0] for r in c.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename='incidents'"))}
        self.assertIn("occurred_at", columns)
        self.assertIn("ix_incidents_occurred_at", indexes)

    def test_it_is_nullable_so_existing_rows_needed_no_rewrite(self):
        with engine.connect() as c:
            nullable = c.execute(text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='incidents' AND column_name='occurred_at'"
            )).scalar()
        self.assertEqual(nullable, "YES")

    def test_the_guard_and_the_migration_agree(self):
        import pathlib

        guard = pathlib.Path("app/main.py").read_text()
        start = guard.index("def ensure_incident_occurred_at")
        body = guard[start:guard.index("def ensure_company_domain_schema")]
        migration = pathlib.Path(
            "alembic/versions/x5y6z7a8b9c0_incident_occurred_at.py"
        ).read_text()
        for shared in ("occurred_at", "ix_incidents_occurred_at"):
            self.assertIn(shared, body)
            self.assertIn(shared, migration)
        for writing in ("UPDATE ", "INSERT ", "DELETE "):
            self.assertNotIn(writing, body.upper())


if __name__ == "__main__":
    unittest.main()
