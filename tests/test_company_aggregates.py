"""Dashboard figures, and the handover they must split on.

Document 2 section 7: "filter source rows BEFORE GROUP BY and before joining
dimension tables; never calculate fleet-wide total then filter displayed
vessel labels." The mistake that rule prevents is invisible in the output --
a fleet-wide total with the labels filtered looks exactly like a correct
scoped total, and is a leak.

These cover TEN-01, TEN-02, TEN-03, TEN-05, TEN-09 and TEN-19 from the
Document 2 matrix, against rows that straddle a handover.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy import text

from app.db.models.cab_booking import CabBooking
from app.db.models.shipping_company import ShippingCompany, VesselRoleClaim
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel import Vessel
from app.db.session import SessionLocal
from app.services import company_aggregates as agg
from app.services.company_metrics import DEFAULT_SUPPRESSION_THRESHOLD

HANDOVER = datetime(2026, 7, 1, tzinfo=timezone.utc)
MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)
SEPT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _uniq(p):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class HandoverTests(unittest.TestCase):
    """Two companies, one vessel, a clean handover on 1 July."""

    def setUp(self):
        self.db = SessionLocal()
        self.vessel = Vessel(name=_uniq("MV"), imo_number=_uniq("IMO"),
                             vessel_type="Bulk Carrier", status="Active")
        self.other_vessel = Vessel(name=_uniq("MV"), imo_number=_uniq("IMO"),
                                   vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([self.vessel, self.other_vessel])
        a, b = _uniq("Out"), _uniq("In")
        self.outgoing = ShippingCompany(legal_name=a, normalized_name=a.upper(),
                                        status="VERIFIED")
        self.incoming = ShippingCompany(legal_name=b, normalized_name=b.upper(),
                                        status="VERIFIED")
        self.db.add_all([self.outgoing, self.incoming])
        self.db.flush()

        self._claim(self.outgoing, datetime(2026, 1, 1, tzinfo=timezone.utc), HANDOVER)
        self._claim(self.incoming, HANDOVER, None)

        # Enough rows on each side that the suppression threshold does not
        # hide what these tests are about.
        self.before = [self._booking(MARCH + timedelta(days=i))
                       for i in range(DEFAULT_SUPPRESSION_THRESHOLD + 1)]
        self.on_boundary = self._booking(HANDOVER)
        self.after = [self._booking(SEPT + timedelta(days=i))
                      for i in range(DEFAULT_SUPPRESSION_THRESHOLD)]
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        for stmt, params in (
            (
                "DELETE FROM cab_bookings WHERE vessel_id = ANY(:v)",
                {"v": [self.vessel.id, self.other_vessel.id]},
            ),
            (
                "DELETE FROM vessel_role_claims WHERE vessel_id = ANY(:v)",
                {"v": [self.vessel.id, self.other_vessel.id]},
            ),
            (
                "DELETE FROM shipping_companies WHERE id = ANY(:v)",
                {"v": [self.outgoing.id, self.incoming.id]},
            ),
            (
                "DELETE FROM vessel_calls WHERE vessel_id = ANY(:v)",
                {"v": [self.vessel.id, self.other_vessel.id]},
            ),
            (
                "DELETE FROM vessels WHERE id = ANY(:v)",
                {"v": [self.vessel.id, self.other_vessel.id]},
            ),
        ):
            self.db.execute(text(stmt), params)
        self.db.commit()
        self.db.close()

    def _claim(self, company, start, end, vessel=None, role="ISM_MANAGER"):
        claim = VesselRoleClaim(
            company_id=company.id, vessel_id=(vessel or self.vessel).id, role=role,
            effective_start=start, effective_end=end, status="APPROVED")
        self.db.add(claim)
        self.db.flush()
        return claim

    def _booking(self, moment, vessel=None):
        vessel = vessel or self.vessel
        call = self.db.query(VesselCall).filter_by(vessel_id=vessel.id).first()
        if call is None:
            call = VesselCall(
                vessel_id=vessel.id,
                vessel_name=vessel.name,
                agency_name="Other",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            self.db.add(call)
            self.db.flush()
        row = CabBooking(
            vessel_call_id=call.id,
            context_resolution="CONFIRMED_UNMANAGED",
            booking_id=_uniq("CAB"),
            vessel_id=(vessel or self.vessel).id,
            pickup_address="Gate",
            pickup_lat=0,
            pickup_lng=0,
            drop_address="City",
            drop_lat=0,
            drop_lng=0,
            vehicle_type="ac",
            vehicle_name="Sedan",
            estimated_price=100,
            distance_km=5,
            status="completed",
            created_at=moment,
            trip_started_at=moment + timedelta(minutes=20),
            trip_completed_at=moment + timedelta(hours=1),
        )
        self.db.add(row)
        self.db.flush()
        return row

    # --- the matrix ---------------------------------------------------------

    def test_the_incoming_company_cannot_see_march(self):
        """TEN-01."""
        figure = agg.booking_activity(self.db, company_id=self.incoming.id)
        self.assertEqual(figure.value, len(self.after) + 1)   # September + boundary

    def test_the_outgoing_company_cannot_see_september(self):
        """TEN-02."""
        figure = agg.booking_activity(self.db, company_id=self.outgoing.id)
        self.assertEqual(figure.value, len(self.before))

    def test_the_boundary_event_belongs_to_exactly_one_of_them(self):
        """TEN-03. Half-open [start, end): the incoming company gets it."""
        out = agg.booking_activity(self.db, company_id=self.outgoing.id).value
        inn = agg.booking_activity(self.db, company_id=self.incoming.id).value
        self.assertEqual(out + inn, len(self.before) + len(self.after) + 1)

    def test_no_inherited_history(self):
        """TEN-09: currently managed, but tenure starts later."""
        self.assertEqual(
            agg.verified_vessels(self.db, company_id=self.incoming.id, at=MARCH).value, 0)
        self.assertEqual(
            agg.verified_vessels(self.db, company_id=self.incoming.id, at=SEPT).value, 1)

    def test_a_vessel_the_company_never_held_contributes_nothing(self):
        for _ in range(DEFAULT_SUPPRESSION_THRESHOLD + 2):
            self._booking(MARCH, vessel=self.other_vessel)
        self.db.commit()
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.outgoing.id).value,
            len(self.before),
        )

    def test_two_roles_on_one_vessel_do_not_double_count(self):
        """TEN-05, and section 22's "avoid double count across roles"."""
        before = agg.booking_activity(self.db, company_id=self.outgoing.id).value
        self._claim(self.outgoing, datetime(2026, 1, 1, tzinfo=timezone.utc),
                    HANDOVER, role="REGISTERED_OWNER")
        self.db.commit()
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.outgoing.id).value, before)

    def test_verified_vessels_counts_the_ship_once_per_company(self):
        self._claim(self.outgoing, datetime(2026, 1, 1, tzinfo=timezone.utc),
                    HANDOVER, role="REGISTERED_OWNER")
        self.db.commit()
        self.assertEqual(
            agg.verified_vessels(self.db, company_id=self.outgoing.id, at=MARCH).value, 1)

    def test_a_revoked_claim_stops_contributing(self):
        self.db.query(VesselRoleClaim).filter(
            VesselRoleClaim.company_id == self.outgoing.id
        ).update({"revoked_at": datetime.now(timezone.utc)})
        self.db.commit()
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.outgoing.id).value, 0)

    # --- suppression, TEN-19 -------------------------------------------------

    def test_small_operational_booking_total_is_not_a_sensitive_person_cohort(self):
        tiny = ShippingCompany(
            legal_name=_uniq("Tiny"),
            normalized_name=_uniq("TINY").upper(),
            status="VERIFIED",
        )
        self.db.add(tiny)
        self.db.flush()
        self._claim(tiny, SEPT, None, vessel=self.other_vessel)
        self._booking(SEPT + timedelta(days=1), vessel=self.other_vessel)
        self.db.commit()
        try:
            figure = agg.booking_activity(self.db, company_id=tiny.id)
            self.assertFalse(figure.suppressed)
            self.assertEqual(figure.value, 1)
            self.assertEqual(figure.display, "1")
        finally:
            self.db.execute(
                text("DELETE FROM vessel_role_claims WHERE company_id = :c"),
                {"c": tiny.id},
            )
            self.db.execute(text("DELETE FROM shipping_companies WHERE id = :c"),
                            {"c": tiny.id})
            self.db.commit()

    def test_zero_is_not_withheld(self):
        """Hiding "none happened" tells the reader nothing and hides nothing."""
        self.db.query(VesselRoleClaim).filter(
            VesselRoleClaim.company_id == self.outgoing.id
        ).update({"revoked_at": datetime.now(timezone.utc)})
        self.db.commit()
        figure = agg.booking_activity(self.db, company_id=self.outgoing.id)
        self.assertFalse(figure.suppressed)
        self.assertEqual(figure.display, "0")

    def test_a_withheld_figure_and_a_zero_do_not_look_the_same(self):
        """The distinction the whole Figure type exists for."""
        self.assertNotEqual(
            agg.Figure(metric=agg.metric("booking_activity"), value=None, suppressed=True).display,
            agg.Figure(metric=agg.metric("booking_activity"), value=0).display,
        )

    def test_an_unavailable_metric_reads_not_available(self):
        figure = agg.Figure(metric=agg.metric("sign_on_off"), value=None, available=False)
        self.assertEqual(figure.display, "Not available")

    def test_every_figure_carries_its_caveat(self):
        figure = agg.booking_activity(self.db, company_id=self.outgoing.id)
        self.assertIn("not a trip", figure.caveat.lower())


if __name__ == "__main__":
    unittest.main()
