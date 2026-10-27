"""Which shore leave card a crew member sees, and whether they may book a cab.

Four situations, and the difference between two of them is the whole point:

    assignment + eligible          APPROVED      everything open
    assignment + not eligible      NOT_ELIGIBLE  cab locked
    no assignment, agency vessel   PENDING       cab locked
    no assignment, other vessel    no card       everything open

PENDING is not an assignment. It says an agency runs this ship and has not
added you yet — waiting on someone, not permitted. Treating it as permission is
exactly the inference that put one ship's records under another, so bookings and
shore passes stay refused until a real assignment exists, and the card turns
into APPROVED or NOT_ELIGIBLE the moment one does.

The last row is why the selection is stored at all: with no assignment, only the
vessel they picked distinguishes "waiting on my agent" from "no agency operates
here", and it has to survive a refresh.

Runs against the configured database inside a transaction that is always
rolled back.
"""

from datetime import datetime, timedelta, timezone
import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


NOW = datetime.now(timezone.utc)


class ShoreLeaveStatusTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent_user = User(email=_uniq("agent") + "@example.com",
                          hashed_password="x", role="agent")
        self.db.add(agent_user)
        self.db.flush()
        self.agency = AgentProfile(
            user_id=agent_user.id, agency_name=_uniq("Agency"),
            location="Port", assigned_port="port_test",
        )
        self.db.add(self.agency)
        self.db.flush()
        self.agent_user = agent_user

        crew_user = User(email=_uniq("crew") + "@example.com",
                         hashed_password="x", role="crew")
        self.db.add(crew_user)
        self.db.flush()
        self.profile = CrewProfile(
            user_id=crew_user.id, full_name="Crew", rank="able_seaman",
            nationality="IN", hpid=_uniq("HP"),
        )
        self.db.add(self.profile)
        self.db.flush()
        self.crew = SimpleNamespace(id=crew_user.id, role="crew")

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def vessel(self, *, agency_name):
        v = Vessel(
            agent_id=self.agent_user.id, name=_uniq("MV"),
            imo_number=_uniq("IMO"), vessel_type="Bulk Carrier",
            status="Active", agency_name=agency_name,
            eta=NOW - timedelta(days=1), etd=NOW + timedelta(days=3),
        )
        self.db.add(v)
        self.db.flush()
        return v

    def assign(self, vessel, *, eligible):
        call = VesselCall(
            vessel_id=vessel.id, agency_id=self.agency.id,
            vessel_name=vessel.name, imo_number=vessel.imo_number,
            port_name="port_test", status="ACTIVE",
            eta=vessel.eta, etd=vessel.etd,
        )
        self.db.add(call)
        self.db.flush()
        self.db.add(CrewAssignment(
            vessel_call_id=call.id, crew_profile_id=self.profile.id,
            crew_name=self.profile.full_name, hpid=self.profile.hpid,
            shore_pass_eligible=eligible, started_at=NOW,
        ))
        self.db.flush()
        return call

    def card(self):
        from app.api.v1.routes_crew import get_crew_profile
        return get_crew_profile(db=self.db, current_user=self.crew)

    def select(self, vessel):
        self.profile.selected_vessel_id = vessel.id
        self.profile.vessel = vessel.name
        self.db.flush()

    def test_assigned_and_eligible_is_approved_with_everything_open(self):
        vessel = self.vessel(agency_name=self.agency.agency_name)
        self.assign(vessel, eligible=True)

        result = self.card()

        self.assertEqual(result.shore_leave_status, "ELIGIBLE")
        self.assertFalse(result.cab_booking_locked)

    def test_assigned_but_not_eligible_locks_the_cab(self):
        vessel = self.vessel(agency_name=self.agency.agency_name)
        self.assign(vessel, eligible=False)

        result = self.card()

        self.assertEqual(result.shore_leave_status, "NOT_ELIGIBLE")
        self.assertTrue(result.cab_booking_locked)

    def test_no_assignment_on_an_agency_vessel_is_pending(self):
        """Waiting on an agent who has not uploaded the manifest."""
        self.select(self.vessel(agency_name=self.agency.agency_name))

        result = self.card()

        self.assertEqual(result.shore_leave_status, "PENDING")
        self.assertTrue(result.cab_booking_locked)

    def test_pending_is_not_an_assignment(self):
        """The card says who they are waiting for, and grants nothing."""
        self.select(self.vessel(agency_name=self.agency.agency_name))

        result = self.card()

        self.assertEqual(result.mapping_status, "Unmapped")
        self.assertFalse(result.shore_pass_eligible)

    def test_no_assignment_on_a_vessel_no_agency_runs_shows_no_card(self):
        """Shore leave is not something they are waiting for."""
        self.select(self.vessel(agency_name="Other"))

        result = self.card()

        self.assertIsNone(result.shore_leave_status)
        self.assertFalse(result.cab_booking_locked)

    def test_selecting_nothing_shows_no_card(self):
        result = self.card()

        self.assertIsNone(result.shore_leave_status)
        self.assertFalse(result.cab_booking_locked)

    def test_pending_becomes_approved_once_the_agent_adds_them(self):
        """The transition the refresh action exists for."""
        vessel = self.vessel(agency_name=self.agency.agency_name)
        self.select(vessel)
        self.assertEqual(self.card().shore_leave_status, "PENDING")

        self.assign(vessel, eligible=True)

        self.assertEqual(self.card().shore_leave_status, "ELIGIBLE")

    def test_pending_becomes_not_eligible_when_that_is_the_decision(self):
        vessel = self.vessel(agency_name=self.agency.agency_name)
        self.select(vessel)
        self.assertEqual(self.card().shore_leave_status, "PENDING")

        self.assign(vessel, eligible=False)

        self.assertEqual(self.card().shore_leave_status, "NOT_ELIGIBLE")

    def test_the_assignment_wins_over_the_vessel_they_picked(self):
        """Picking another ship does not move them off their own.

        Selection is a label; the manifest is the authority.
        """
        assigned = self.vessel(agency_name=self.agency.agency_name)
        self.assign(assigned, eligible=True)
        self.select(self.vessel(agency_name="Other"))

        result = self.card()

        self.assertEqual(result.shore_leave_status, "ELIGIBLE")
        self.assertEqual(result.vessel, assigned.name)


if __name__ == "__main__":
    unittest.main()
