"""The vessel crew-profile view must not return another agency's history.

Appearing on an agent's current manifest authorises the crew member, not the
crew member's whole past. These tests pin the agency scoping of the bookings
and port visits that GET /vessels/crew/{hp_id}/profile returns.
"""
import unittest
import uuid
from datetime import datetime, timedelta

import app.db.base  # noqa: F401
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_vessels import get_crew_profile
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class CrewProfileHistoryScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.rival = User(email=_uniq("rival") + "@example.com", hashed_password="x", role="agent")
        self.admin = User(email=_uniq("admin") + "@example.com", hashed_password="x", role="superadmin")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([self.agent, self.rival, self.admin, crew_user])
        self.db.flush()

        self.agency = AgentProfile(user_id=self.agent.id, agency_name=_uniq("Agency A"), location="Port A")
        self.rival_agency = AgentProfile(user_id=self.rival.id, agency_name=_uniq("Agency B"), location="Port B")
        self.profile = CrewProfile(
            user_id=crew_user.id, full_name="Shared Crew", rank="able_seaman",
            nationality="IN", hpid=_uniq("HP"),
        )
        self.db.add_all([self.agency, self.rival_agency, self.profile])
        self.db.flush()

        # The crew member is on this agent's current manifest, which is what
        # made the unscoped history reachable in the first place.
        self.vessel = Vessel(
            agent_id=self.agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier",
        )
        self.rival_vessel = Vessel(
            agent_id=self.rival.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier",
        )
        self.db.add_all([self.vessel, self.rival_vessel])
        self.db.flush()
        self.db.add(VesselCrew(
            vessel_id=self.vessel.id, name="Shared Crew", rank="able_seaman",
            hp_id=self.profile.hpid,
        ))

        now = datetime.utcnow()
        self.mine_old = self._booking("MINE-OLD", self.agency.id, now - timedelta(days=2))
        self.mine_new = self._booking("MINE-NEW", self.agency.id, now)
        self.theirs = self._booking("THEIRS", self.rival_agency.id, now - timedelta(days=1))
        self.unstamped = self._booking("UNSTAMPED", None, now - timedelta(days=3))
        self.db.flush()

        # One port visit per agency, each tied to that agency's own vessel call.
        self.db.add_all([
            self._pass("MY PORT", self.vessel.id),
            self._pass("THEIR PORT", self.rival_vessel.id),
        ])
        self.db.flush()

    def _booking(self, tag, agency_id, created):
        booking = CabBooking(
            booking_id=_uniq(tag), crew_id=self.profile.id, agency_id=agency_id,
            pickup_address=f"{tag} pickup", pickup_lat=17.7, pickup_lng=83.3,
            drop_address=f"{tag} drop", drop_lat=17.71, drop_lng=83.31,
            vehicle_type="ac", vehicle_name="Sedan", estimated_price=100,
            distance_km=5, status=BookingStatus.COMPLETED, created_at=created,
        )
        self.db.add(booking)
        return booking

    def _pass(self, port, vessel_id):
        call = VesselCall(vessel_id=vessel_id, vessel_name="MV", port_name=port)
        self.db.add(call)
        self.db.flush()
        return ShorePass(
            crew_profile_id=self.profile.id, vessel_call_id=call.id,
            shore_pass_id=_uniq("SP"), port_name=port,
        )

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def _addresses(self, result):
        return {b.pickup_address for b in result["bookings"]}

    def test_agent_sees_only_its_own_agency_bookings(self):
        result = get_crew_profile(hp_id=self.profile.hpid, current_user=self.agent, db=self.db)
        self.assertEqual(self._addresses(result), {"MINE-OLD pickup", "MINE-NEW pickup"})

    def test_agent_never_sees_another_agency_or_unstamped_bookings(self):
        result = get_crew_profile(hp_id=self.profile.hpid, current_user=self.agent, db=self.db)
        addresses = self._addresses(result)
        self.assertNotIn("THEIRS pickup", addresses)
        # No immutable agency stamp means superadmin-only until reconciled,
        # matching the rule the incident routes already apply.
        self.assertNotIn("UNSTAMPED pickup", addresses)

    def test_visible_bookings_stay_newest_first(self):
        result = get_crew_profile(hp_id=self.profile.hpid, current_user=self.agent, db=self.db)
        self.assertEqual(
            [b.pickup_address for b in result["bookings"]],
            ["MINE-NEW pickup", "MINE-OLD pickup"],
        )

    def test_agent_sees_only_its_own_port_visits(self):
        result = get_crew_profile(hp_id=self.profile.hpid, current_user=self.agent, db=self.db)
        self.assertEqual(result["visits"], ["MY PORT"])

    def test_superadmin_still_sees_everything(self):
        result = get_crew_profile(hp_id=self.profile.hpid, current_user=self.admin, db=self.db)
        self.assertEqual(len(result["bookings"]), 4)
        self.assertEqual(sorted(result["visits"]), ["MY PORT", "THEIR PORT"])

    def test_unrelated_agent_cannot_tell_the_crew_member_apart_from_a_miss(self):
        with self.assertRaises(HTTPException) as denied:
            get_crew_profile(hp_id=self.profile.hpid, current_user=self.rival, db=self.db)
        with self.assertRaises(HTTPException) as missing:
            get_crew_profile(hp_id="HP-NOT-FOUND", current_user=self.rival, db=self.db)
        self.assertEqual(denied.exception.status_code, 404)
        self.assertEqual(denied.exception.detail, missing.exception.detail)


if __name__ == "__main__":
    unittest.main()
