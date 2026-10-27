"""The three TEN cases that had no coverage, because reports did not exist.

Document 2 §13 leaves three of its twenty-two authorization cases untested for
one reason — the thing they test was not built:

    TEN-04  a scoped annual report
    TEN-07  a stale cached report after a retroactive shortening
    TEN-14  an export after revocation

All three are about the same mistake in different clothes: a report is a
*rendering* of entitlement, so anything that stores one, or that scopes it by
period instead of by tenure, hands a company data it does not hold.

The design answer is that nothing is stored. Every report and export is
computed when it is asked for, so TEN-07 has no cache to go stale and TEN-14
takes effect on the next request. These tests exist to keep it that way: each
one fails if a cache is introduced or if the period ever replaces tenure
rather than narrowing it.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import routes_shipping_company as company_routes
from app.db.models.cab_booking import BookingStatus, CabBooking, VehicleType
from app.db.models.crew_profile import CrewProfile
from app.db.models.shipping_company import (
    CompanyUserMembership, ShippingCompany, VesselRoleClaim,
)
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.session import engine

YEAR_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
YEAR_END = datetime(2027, 1, 1, tzinfo=timezone.utc)
MARCH = datetime(2026, 3, 15, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 15, tzinfo=timezone.utc)
JULY = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class CompanyReportTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.user = User(email=_uniq("viewer") + "@example.com", hashed_password="x", role="crew")
        self.db.add(self.user)
        self.db.flush()

        self.vessel = Vessel(name=_uniq("MV"), imo_number=_uniq("IMO"),
                             vessel_type="Bulk Carrier", status="Active")
        self.db.add(self.vessel)
        self.db.flush()

        name = _uniq("Company")
        self.company = ShippingCompany(legal_name=name, normalized_name=name.upper(),
                                       status="VERIFIED")
        self.db.add(self.company)
        self.db.flush()
        self.db.add(CompanyUserMembership(company_id=self.company.id, user_id=self.user.id,
                                          role="VIEWER", enabled=True))

        # Held from January to July. September is somebody else's.
        self.claim = VesselRoleClaim(
            company_id=self.company.id, vessel_id=self.vessel.id, role="ISM",
            effective_start=YEAR_START, effective_end=JULY, status="APPROVED",
        )
        self.db.add(self.claim)
        self.db.flush()

        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add(crew_user)
        self.db.flush()
        self.crew = CrewProfile(user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                                nationality="IN", hpid=_uniq("HP"))
        self.db.add(self.crew)
        self.db.flush()

        # Five each side, because the suppression threshold is 5: a cohort of
        # one is withheld, and "withheld" would hide the very difference these
        # tests are about.
        self.inside = [self._booking(MARCH) for _ in range(5)]
        self.outside = [self._booking(SEPTEMBER) for _ in range(5)]
        self.db.flush()

        self.context = (self.company, None)

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def _booking(self, when):
        booking = CabBooking(
            booking_id=_uniq("CAB"), crew_id=self.crew.id, vessel_id=self.vessel.id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type=VehicleType.AC, vehicle_name="Sedan", estimated_price=100,
            distance_km=5, status=BookingStatus.COMPLETED, created_at=when,
        )
        self.db.add(booking)
        self.db.flush()
        # created_at has a server default, so it has to be set after the insert.
        booking.created_at = when
        self.db.flush()
        return booking

    def report(self, start=YEAR_START, end=YEAR_END):
        return company_routes.company_report(
            period_start=start, period_end=end, db=self.db, context=self.context)

    def figure(self, report, key):
        return next(f for f in report.figures if f.key == key)

    # --- TEN-04: a scoped annual report ------------------------------------

    def test_an_annual_report_contains_only_the_months_the_company_held(self):
        """The year is the period. Tenure is still what decides each row."""
        figure = self.figure(self.report(), "booking_activity")
        # March is inside the claim; September is after it. Five, not ten.
        self.assertEqual(figure.value, 5)
        self.assertFalse(figure.suppressed)

    def test_a_period_that_misses_the_tenure_returns_nothing(self):
        """Narrowing, never widening: the period cannot add rows tenure excludes."""
        report = self.report(start=JULY, end=YEAR_END)
        self.assertIn(self.figure(report, "booking_activity").value, (0, None))

    def test_the_period_boundary_belongs_to_exactly_one_report(self):
        """Half-open, like the tenure intervals it composes with."""
        before = self.report(start=YEAR_START, end=MARCH)
        including = self.report(start=YEAR_START, end=MARCH + timedelta(seconds=1))
        self.assertIn(self.figure(before, "booking_activity").value, (0, None))
        self.assertEqual(self.figure(including, "booking_activity").value, 5)

    def test_a_backwards_period_is_refused(self):
        with self.assertRaises(HTTPException) as error:
            self.report(start=YEAR_END, end=YEAR_START)
        self.assertEqual(error.exception.status_code, 422)

    # --- TEN-07: no cache, so nothing can go stale -------------------------

    def test_shortening_a_claim_changes_the_next_report_immediately(self):
        self.assertEqual(self.figure(self.report(), "booking_activity").value, 5)

        # Retroactively shortened to February: March is no longer theirs.
        self.claim.effective_end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.db.flush()

        self.assertIn(self.figure(self.report(), "booking_activity").value, (0, None))

    def test_the_report_says_it_was_computed_rather_than_stored(self):
        report = self.report()
        self.assertTrue(report.computed_on_read)
        self.assertGreater(report.generated_at, YEAR_START)

    def test_no_report_row_is_written(self):
        """A snapshot of tenure-scoped data is the TEN-07 defect itself."""
        from app.db.models.report_snapshot import ReportSnapshot

        before = self.db.query(ReportSnapshot).count()
        self.report()
        company_routes.company_report_export(
            period_start=YEAR_START, period_end=YEAR_END,
            db=self.db, context=self.context)
        self.assertEqual(self.db.query(ReportSnapshot).count(), before)

    # --- TEN-14: export after revocation -----------------------------------

    def test_revoking_a_claim_empties_the_export(self):
        before = {row[1]: row[3] for row in self.export_rows()}
        self.assertEqual(before["booking_activity"], "5")

        self.claim.revoked_at = datetime.now(timezone.utc)
        self.db.flush()

        after = {row[1]: row[3] for row in self.export_rows()}
        self.assertEqual(after["booking_activity"], "0")

    def export_rows(self):
        import csv
        import io

        response = company_routes.company_report_export(
            period_start=YEAR_START, period_end=YEAR_END,
            db=self.db, context=self.context)
        text = response.body.decode("utf-8")
        return list(csv.reader(io.StringIO(text)))[1:]

    def test_the_export_keeps_withheld_and_no_source_apart(self):
        """A spreadsheet that writes a blank for both has merged two answers."""
        states = {row[1]: row[4] for row in self.export_rows()}
        self.assertIn("no_source", states.values())
        self.assertEqual(states["booking_activity"], "counted")

    def test_the_export_is_not_cacheable(self):
        response = company_routes.company_report_export(
            period_start=YEAR_START, period_end=YEAR_END,
            db=self.db, context=self.context)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertIn("attachment", response.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
