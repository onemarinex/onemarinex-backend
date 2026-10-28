"""Trip Monitoring must bucket trips by where the driver actually is.

The agent's Trips screen has three tabs, and the spec ties them to the driver's
own actions:

    requested  from the trip being raised until the driver reaches pickup
    on going   from the driver reaching pickup until the ride is completed
    completed  once the driver marks the ride complete

Previously `arrived` and `on_trip` appeared in no bucket at all, so a ride that
was genuinely underway vanished from every tab, and `driver_assigned` /
`driver_accepted` were shown as on-going when the driver was still en route.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from sqlalchemy import text

from app.api.v1.routes_trips import get_trip_monitoring
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.db.models.agent_profile import AgentProfile
from tests.context_fixtures import immutable_call
from app.services.assignment_ref import issue_assignment_ref
from app.services.booking_service import list_bookings_for_user


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class TripMonitoringBucketTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([agent, crew_user])
        self.db.flush()

        hpid = _uniq("HP")
        self.crew = CrewProfile(
            user_id=crew_user.id, full_name="Test Crew", rank="AB", nationality="IN", hpid=hpid
        )
        vessel = Vessel(
            agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add_all([self.crew, vessel])
        self.db.flush()
        self.db.add(VesselCrew(vessel_id=vessel.id, name="Test Crew", rank="AB", hp_id=hpid))
        self.db.flush()

        profile = AgentProfile(
            user_id=agent.id, agency_name=_uniq("Agency"), location="Fixture"
        )
        self.db.add(profile)
        self.db.flush()
        self.fixture_call = immutable_call(self.db, vessel, profile.id)
        self.agent_user = SimpleNamespace(id=agent.id, role="agent", agent_profile=None)

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def add_trip(self, status, created_at=None):
        trip = CabBooking(
            vessel_id=self.fixture_call.vessel_id,
            agency_id=self.fixture_call.agency_id,
            vessel_call_id=self.fixture_call.id,
            context_resolution="CONFIRMED_AGENCY",
            booking_id=_uniq("CAB"),
            crew_id=self.crew.id,
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
            status=status,
            created_at=created_at or datetime.utcnow(),
        )
        self.db.add(trip)
        self.db.flush()
        return trip

    def buckets(self):
        r = get_trip_monitoring(db=self.db, current_user=self.agent_user)
        return {"ongoing": len(r.ongoing), "requested": len(r.requested), "completed": len(r.completed)}

    def test_driver_en_route_counts_as_requested_not_ongoing(self):
        self.add_trip(BookingStatus.DRIVER_ASSIGNED)
        self.add_trip(BookingStatus.DRIVER_ACCEPTED)

        self.assertEqual(self.buckets(), {"ongoing": 0, "requested": 2, "completed": 0})

    def test_arrived_and_on_trip_are_ongoing(self):
        # These previously matched no bucket and disappeared from the screen.
        self.add_trip(BookingStatus.ARRIVED)
        self.add_trip(BookingStatus.ON_TRIP)

        self.assertEqual(self.buckets(), {"ongoing": 2, "requested": 0, "completed": 0})

    def test_legacy_in_progress_still_counts_as_ongoing(self):
        self.add_trip(BookingStatus.IN_PROGRESS)

        self.assertEqual(self.buckets()["ongoing"], 1)

    def test_pending_states_are_requested(self):
        self.add_trip(BookingStatus.PENDING_PROVIDER_RESPONSE)
        self.add_trip(BookingStatus.PROVIDER_ACCEPTED)

        self.assertEqual(self.buckets(), {"ongoing": 0, "requested": 2, "completed": 0})

    def test_completed_is_completed(self):
        self.add_trip(BookingStatus.COMPLETED)

        self.assertEqual(self.buckets(), {"ongoing": 0, "requested": 0, "completed": 1})

    def test_cancelled_trips_are_not_shown(self):
        self.add_trip(BookingStatus.CANCELLED)
        self.add_trip(BookingStatus.PROVIDER_REJECTED)

        self.assertEqual(self.buckets(), {"ongoing": 0, "requested": 0, "completed": 0})

    def test_no_live_trip_is_ever_dropped(self):
        """The regression that started this: every live status must appear somewhere."""
        live = [
            BookingStatus.PENDING_PROVIDER_RESPONSE,
            BookingStatus.PROVIDER_ACCEPTED,
            BookingStatus.DRIVER_ASSIGNED,
            BookingStatus.DRIVER_ACCEPTED,
            BookingStatus.ARRIVED,
            BookingStatus.ON_TRIP,
        ]
        for s in live:
            self.add_trip(s)

        counts = self.buckets()
        self.assertEqual(
            counts["ongoing"] + counts["requested"] + counts["completed"],
            len(live),
            f"a live trip is missing from every tab: {counts}",
        )

    def test_each_tab_is_newest_first(self):
        older = self.add_trip(
            BookingStatus.ON_TRIP, datetime.utcnow() - timedelta(hours=1)
        )
        newer = self.add_trip(BookingStatus.ON_TRIP, datetime.utcnow())

        result = get_trip_monitoring(db=self.db, current_user=self.agent_user)

        self.assertEqual(
            [trip.booking_id for trip in result.ongoing],
            [newer.booking_id, older.booking_id],
        )

    def test_general_booking_list_uses_id_as_stable_tiebreaker(self):
        created = datetime.utcnow()
        first = self.add_trip(BookingStatus.ON_TRIP, created)
        second = self.add_trip(BookingStatus.ON_TRIP, created)

        crew_user = self.db.query(User).filter(User.id == self.crew.user_id).one()
        result = list_bookings_for_user(self.db, crew_user)

        ids = [trip.id for trip in result if trip.id in {first.id, second.id}]
        self.assertEqual(ids, [second.id, first.id])


if __name__ == "__main__":
    unittest.main()


class TripMonitoringIdentifierTests(unittest.TestCase):
    """The agent's Trips screen must not be handed the passport.

    The opaque reference replaced hp_id on the driver, aggregator and crew-list
    payloads. This endpoint was missed, and kept sending the legacy identifier,
    which in its old format is `HP-<passport>-<nationality>-<port>`.
    """

    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([agent, crew_user])
        self.db.flush()

        self.legacy_hpid = "HP-P7654321-IND-VIZ"
        crew = CrewProfile(
            user_id=crew_user.id, full_name="Test Crew", rank="AB",
            nationality="IN", hpid=self.legacy_hpid,
        )
        vessel = Vessel(
            agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add_all([crew, vessel])
        self.db.flush()
        self.db.add(VesselCrew(
            vessel_id=vessel.id, name="Test Crew", rank="AB", hp_id=self.legacy_hpid,
        ))

        profile = AgentProfile(
            user_id=agent.id, agency_name=_uniq("Agency"), location="Fixture"
        )
        self.db.add(profile)
        self.db.flush()
        call = VesselCall(
            vessel_id=vessel.id, vessel_name=vessel.name, agency_id=profile.id
        )
        self.db.add(call)
        self.db.flush()
        self.reference = issue_assignment_ref(self.db)
        assignment = CrewAssignment(
            vessel_call_id=call.id, crew_profile_id=crew.id, crew_name="Test Crew",
            rank="AB", hpid=self.legacy_hpid, public_ref=self.reference,
        )
        self.db.add(assignment)
        self.db.flush()

        self.db.add(
            CabBooking(
                booking_id=_uniq("CAB"),
                crew_id=crew.id,
                vessel_id=vessel.id,
                crew_assignment_id=assignment.id,
                vessel_call_id=call.id,
                agency_id=profile.id,
                context_resolution="CONFIRMED_AGENCY",
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
                status=BookingStatus.ON_TRIP,
                created_at=datetime.utcnow(),
            )
        )
        self.db.flush()

        self.agent_user = SimpleNamespace(id=agent.id, role="agent", agent_profile=None)

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def _crew_payload(self):
        result = get_trip_monitoring(db=self.db, current_user=self.agent_user)
        self.assertEqual(len(result.ongoing), 1, "the seeded trip should be on-going")
        return result.ongoing[0].crew_details

    def test_the_opaque_reference_is_carried(self):
        self.assertEqual(self._crew_payload().reference, self.reference)

    def test_the_passport_is_not_what_the_screen_prefers(self):
        """The screen renders `reference || hp_id`, so a present reference wins."""
        crew = self._crew_payload()
        self.assertTrue(crew.reference)
        self.assertNotIn("P7654321", crew.reference)

    def test_a_trip_with_no_assignment_still_answers(self):
        """Pre-assignment rows exist; an empty reference must not break the tab."""
        self.db.execute(
            text("UPDATE cab_bookings SET crew_assignment_id = NULL WHERE crew_id = :c"),
            {"c": self.db.query(CrewProfile).filter(
                CrewProfile.hpid == self.legacy_hpid).one().id},
        )
        self.assertEqual(self._crew_payload().reference, "")
