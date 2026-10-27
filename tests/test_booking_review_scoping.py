"""Booking reviews belonged to whoever knew a booking id.

Both routes checked the caller's *role* and never their relationship to the
booking. A crew member with any completed booking's id could read its reviews
— which carry the reviewer's full name — and could post one, which recomputes
the driver's public rating. So the read leaked a name and the write moved a
number strangers can see.

Addressed by booking id, so the rule has to be the one `get_booking_details`
already uses: owner or actively-invited, and a 404 rather than a 403, because
whether somebody else's booking exists is not an answer either.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_crew import get_booking_reviews, submit_booking_review
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.agent_profile import AgentProfile
from app.db.models.booking_invitation import BookingInvitation
from app.db.models.booking_review import BookingReview
from app.db.models.cab_booking import BookingStatus, CabBooking, VehicleType
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class BookingReviewScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        from app.db.models.port import Port
        self.port_id = self.db.query(Port.id).order_by(Port.id).limit(1).scalar()

        self.owner, self.owner_profile = self.make_crew()
        self.guest, self.guest_profile = self.make_crew()
        self.stranger, self.stranger_profile = self.make_crew()

        self.booking = CabBooking(
            booking_id=_uniq("CAB"), crew_id=self.owner_profile.id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type=VehicleType.AC, vehicle_name="Sedan", estimated_price=100,
            distance_km=5, status=BookingStatus.COMPLETED,
        )
        self.db.add(self.booking)
        self.db.flush()

        self.db.add(BookingInvitation(
            booking_id=self.booking.id,
            invited_crew_id=self.guest_profile.id,
            invited_by_id=self.owner_profile.id,
            status="active",
        ))
        self.db.add(BookingReview(
            booking_id=self.booking.id, crew_id=self.owner_profile.id,
            review_type="facility_stop", facility_stop_id="stop-1",
            facility_name="Cafe", rating=5, review_text="Good",
        ))
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_crew(self):
        user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add(user)
        self.db.flush()
        profile = CrewProfile(user_id=user.id, full_name="Crew Member", rank="able_seaman",
                              nationality="IN", hpid=_uniq("HP"))
        self.db.add(profile)
        self.db.flush()
        return SimpleNamespace(id=user.id, role="crew"), profile

    def read(self, actor):
        return get_booking_reviews(
            booking_id=self.booking.booking_id, db=self.db, current_user=actor
        )

    def write(self, actor):
        return submit_booking_review(
            booking_id=self.booking.booking_id,
            body=SimpleNamespace(
                review_type="facility_stop", rating=4, review_text="Fine",
                facility_stop_id="stop-2", facility_name="Shop", driver_id=None,
            ),
            db=self.db, current_user=actor,
        )

    # --- reading ---

    def test_the_owner_reads_their_own_reviews(self):
        self.assertEqual(len(self.read(self.owner)), 1)

    def test_an_invited_crew_member_reads_them(self):
        self.assertEqual(len(self.read(self.guest)), 1)

    def test_an_uninvolved_crew_member_is_told_nothing(self):
        with self.assertRaises(HTTPException) as error:
            self.read(self.stranger)
        self.assertEqual(error.exception.status_code, 404)
        # Not 403 — that would confirm the booking exists.
        self.assertEqual(error.exception.detail, "Booking not found")

    def test_a_withdrawn_invitation_stops_working(self):
        invitation = self.db.query(BookingInvitation).filter(
            BookingInvitation.booking_id == self.booking.id
        ).one()
        invitation.status = "cancelled"
        self.db.flush()
        with self.assertRaises(HTTPException) as error:
            self.read(self.guest)
        self.assertEqual(error.exception.status_code, 404)

    def test_an_agent_from_another_agency_is_told_nothing(self):
        user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        self.db.add(AgentProfile(user_id=user.id, agency_name="Rival", location="Visakhapatnam"))
        self.db.flush()
        with self.assertRaises(HTTPException) as error:
            self.read(SimpleNamespace(id=user.id, role="agent"))
        self.assertEqual(error.exception.status_code, 404)

    def test_the_booking_s_own_agency_may_read(self):
        user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        profile = AgentProfile(user_id=user.id, agency_name="Theirs", location="Visakhapatnam")
        self.db.add(profile)
        self.db.flush()
        self.booking.agency_id = profile.id
        self.db.flush()
        self.assertEqual(len(self.read(SimpleNamespace(id=user.id, role="agent"))), 1)

    def test_an_aggregator_that_is_not_the_provider_is_told_nothing(self):
        user = User(email=_uniq("agg") + "@example.com", hashed_password="x", role="aggregator")
        self.db.add(user)
        self.db.flush()
        self.db.add(AggregatorProfile(user_id=user.id, company_name="Other Cabs",
                                          operating_port_id=self.port_id))
        self.db.flush()
        with self.assertRaises(HTTPException) as error:
            self.read(SimpleNamespace(id=user.id, role="aggregator"))
        self.assertEqual(error.exception.status_code, 404)

    def test_superadmin_may_read(self):
        actor = SimpleNamespace(id=self.owner.id, role="superadmin")
        self.assertEqual(len(self.read(actor)), 1)

    # --- writing ---

    def test_the_owner_may_review_their_trip(self):
        self.assertEqual(self.write(self.owner)["rating"], 4)

    def test_an_uninvolved_crew_member_cannot_review_someone_else_s_trip(self):
        with self.assertRaises(HTTPException) as error:
            self.write(self.stranger)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(
            self.db.query(BookingReview).filter(
                BookingReview.crew_id == self.stranger_profile.id
            ).count(), 0,
        )


if __name__ == "__main__":
    unittest.main()
