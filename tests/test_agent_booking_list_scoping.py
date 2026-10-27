"""The bookings list was broader than the detail check beside it.

Audit R04 names `_agent_may_view_booking` and `_agent_owns_trip`: both fall
back to current vessel/crew linkage when `CabBooking.agency_id` is null, and
the audit says not to remove that fallback before measuring its impact.

A delegated search for the same *shape* turned up `get_agent_bookings`, which
the audit did not name and which was looser still. Its legacy arm asked only:

    agency_id IS NULL AND crew_id IN (crew on my current manifests)

with no condition on the booking's own vessel. So a booking for a ship this
agency has never operated appeared in the list -- with its ride OTP in the
payload -- while opening that same booking by id was refused, because the
detail check requires the stored vessel to be one of theirs.

The fallback is not removed here; it is narrowed to the rule the audit already
examined, which removes an inconsistency rather than making a policy decision
the measurement has not been done for.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.api.v1.routes_agents import get_agent_bookings
from app.api.v1.routes_bookings import _agent_may_view_booking
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking, VehicleType
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class AgentBookingListScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.agent = self.make_agent()
        self.rival = self.make_agent()

        # One crew member, on this agent's current manifest.
        self.hpid = _uniq("HP")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add(crew_user)
        self.db.flush()
        self.crew = CrewProfile(user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                                nationality="IN", hpid=self.hpid)
        self.db.add(self.crew)
        self.db.flush()
        self.db.add(VesselCrew(vessel_id=self.agent.vessel.id, name="Crew",
                               rank="able_seaman", hp_id=self.hpid))
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def make_agent(self):
        user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        profile = AgentProfile(user_id=user.id, agency_name=_uniq("Agency"),
                               location="Visakhapatnam")
        vessel = Vessel(agent_id=user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([profile, vessel])
        self.db.flush()
        return SimpleNamespace(user=user, profile=profile, vessel=vessel,
                               id=user.id, role="agent")

    def booking(self, *, agency_id=None, vessel_id=None):
        row = CabBooking(
            booking_id=_uniq("CAB"), crew_id=self.crew.id,
            agency_id=agency_id, vessel_id=vessel_id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type=VehicleType.AC, vehicle_name="Sedan", estimated_price=100,
            distance_km=5, status=BookingStatus.COMPLETED, otp="4821",
        )
        self.db.add(row)
        self.db.flush()
        return row

    def listed(self, agent):
        rows = get_agent_bookings(
            status_filter=None, provider_type=None, date_from=None, date_to=None,
            db=self.db, current_user=agent,
        )
        return {row["booking_id"] for row in rows}

    # --- the inconsistency ---

    def test_a_legacy_booking_for_another_agency_s_vessel_is_not_listed(self):
        """The passenger being on my manifest does not make the voyage mine."""
        stranger = self.booking(vessel_id=self.rival.vessel.id)

        self.assertNotIn(stranger.booking_id, self.listed(self.agent))

    def test_the_list_and_the_detail_check_now_agree(self):
        """They disagreed: listed but not openable, with the OTP in the list."""
        stranger = self.booking(vessel_id=self.rival.vessel.id)

        listed = stranger.booking_id in self.listed(self.agent)
        openable = _agent_may_view_booking(self.db, self.agent.id, stranger)
        self.assertEqual(listed, openable)
        self.assertFalse(openable)

    # --- what must keep working ---

    def test_a_legacy_booking_on_my_own_vessel_is_still_listed(self):
        mine = self.booking(vessel_id=self.agent.vessel.id)

        self.assertIn(mine.booking_id, self.listed(self.agent))
        self.assertTrue(_agent_may_view_booking(self.db, self.agent.id, mine))

    def test_a_legacy_booking_with_no_vessel_still_reaches_the_crew_s_agency(self):
        """The case the fallback exists for, which is not removed here."""
        orphan = self.booking(vessel_id=None)

        self.assertIn(orphan.booking_id, self.listed(self.agent))
        self.assertTrue(_agent_may_view_booking(self.db, self.agent.id, orphan))

    def test_a_booking_with_an_agency_is_scoped_by_it(self):
        mine = self.booking(agency_id=self.agent.profile.id)
        theirs = self.booking(agency_id=self.rival.profile.id)

        listed = self.listed(self.agent)
        self.assertIn(mine.booking_id, listed)
        self.assertNotIn(theirs.booking_id, listed)

    def test_an_agent_with_no_vessels_and_no_crew_sees_nothing(self):
        lonely = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(lonely)
        self.db.flush()
        self.booking(vessel_id=None)

        self.assertEqual(
            self.listed(SimpleNamespace(id=lonely.id, role="agent")), set())


if __name__ == "__main__":
    unittest.main()
