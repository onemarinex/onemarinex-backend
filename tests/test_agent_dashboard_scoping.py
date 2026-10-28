"""The agent dashboard must only ever report the agent's own ships.

Before this, Active Trips was assigned the crew-ashore count, and trips and
incidents were queried with no agent filter at all, so every agent saw
platform-wide totals. These tests build two agents side by side and assert that
neither can see the other's activity.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

from datetime import datetime, timedelta
import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.api.v1.routes_agents import _agent_scope, get_dashboard_data
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from tests.context_fixtures import immutable_call


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class AgentDashboardScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agent(self, *, live_trips: int, crew_ashore: int):
        """An agent with one vessel, one crew member, and some activity."""
        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([agent, crew_user])
        self.db.flush()
        agent_profile = AgentProfile(
            user_id=agent.id, agency_name=_uniq("Agency"), location="Test Port"
        )
        self.db.add(agent_profile)
        self.db.flush()

        hpid = _uniq("HP")
        crew = CrewProfile(
            user_id=crew_user.id, full_name="Test Crew", rank="AB", nationality="IN", hpid=hpid
        )
        vessel = Vessel(
            agent_id=agent.id,
            name=_uniq("MV Test"),
            imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier",
            status="Active",
        )
        self.db.add_all([crew, vessel])
        self.db.flush()

        self.db.add(VesselCrew(vessel_id=vessel.id, name="Test Crew", rank="AB", hp_id=hpid))

        for _ in range(crew_ashore):
            # Ashore means signed out and not signed back in. A pass with no
            # out_time was issued but never used, and must not be counted --
            # that is what made the live tile read 112.
            self.db.add(ShorePass(
                crew_profile_id=crew.id, shore_pass_id=_uniq("SP"),
                out_time=datetime.utcnow() - timedelta(hours=2),
            ))

        for _ in range(live_trips):
            self.db.add(
                CabBooking(
                    booking_id=_uniq("CAB"),
                    crew_id=crew.id,
                    vessel_id=vessel.id,
                    agency_id=agent_profile.id,
                    vessel_call_id=immutable_call(self.db, vessel, agent_profile.id).id,
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
                )
            )

        self.db.flush()
        return SimpleNamespace(id=agent.id, role="agent", agent_profile=agent_profile), vessel

    def dashboard(self, agent_user):
        return get_dashboard_data(db=self.db, current_user=agent_user)

    def test_agent_sees_only_their_own_trips_and_crew(self):
        agent_a, vessel_a = self.make_agent(live_trips=2, crew_ashore=1)
        self.make_agent(live_trips=7, crew_ashore=5)  # a second, unrelated agency

        dashboard = self.dashboard(agent_a)
        stats = dashboard.stats

        self.assertEqual(stats.active_trips, 2, "counted another agency's trips")
        self.assertEqual(stats.crew_in_shore, 1, "counted another agency's crew ashore")
        self.assertEqual(stats.total_vessels, 1)
        card = next(item for item in dashboard.active_vessels if item.id == vessel_a.id)
        self.assertEqual(card.ongoing_trips_count, stats.active_trips)
        self.assertEqual(card.crew_ashore_count, stats.crew_in_shore)

    def test_active_trips_is_not_just_the_crew_ashore_count(self):
        # The original bug: active_trips = crew_in_shore, so the dashboard showed
        # the same number twice. Different values here prove they are independent.
        agent, _ = self.make_agent(live_trips=3, crew_ashore=1)

        stats = self.dashboard(agent).stats

        self.assertEqual(stats.crew_in_shore, 1)
        self.assertEqual(stats.active_trips, 3)
        self.assertNotEqual(stats.active_trips, stats.crew_in_shore)

    def test_on_trip_status_is_counted_as_active(self):
        # ON_TRIP is the current status; only the legacy IN_PROGRESS was checked
        # before, so live trips were invisible.
        agent, _ = self.make_agent(live_trips=1, crew_ashore=0)

        self.assertEqual(self.dashboard(agent).stats.active_trips, 1)

    def test_live_trips_list_is_populated(self):
        # It was hardcoded to [] even though the query ran.
        agent, vessel = self.make_agent(live_trips=2, crew_ashore=0)

        live = self.dashboard(agent).live_trips

        self.assertEqual(len(live), 2)
        self.assertEqual(live[0].vessel_name, vessel.name)
        self.assertEqual(live[0].crew_name, "Test Crew")

    def test_agent_with_no_vessels_gets_zeroes_not_platform_totals(self):
        self.make_agent(live_trips=4, crew_ashore=3)  # somebody else is busy
        lonely = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(lonely)
        self.db.flush()
        agent = SimpleNamespace(id=lonely.id, role="agent", agent_profile=None)

        stats = self.dashboard(agent).stats

        self.assertEqual(
            (stats.total_vessels, stats.active_trips, stats.crew_in_shore, stats.todays_trips),
            (0, 0, 0, 0),
        )

    def test_agent_scope_returns_empty_when_agent_has_no_vessels(self):
        vessel_ids, crew_ids = _agent_scope(self.db, 10**9)
        self.assertEqual((vessel_ids, crew_ids), ([], []))

    def test_departed_vessels_are_not_counted_as_active(self):
        agent, vessel = self.make_agent(live_trips=0, crew_ashore=0)
        vessel.status = "Departed"
        self.db.flush()

        self.assertEqual(self.dashboard(agent).stats.total_vessels, 0)

    def test_per_vessel_and_headline_incidents_use_canonical_vessel_link(self):
        agent, vessel = self.make_agent(live_trips=0, crew_ashore=0)
        agent_profile = self.db.query(AgentProfile).filter(
            AgentProfile.user_id == agent.id
        ).one()
        self.db.add(Incident(
            incident_id=_uniq("INC"),
            type=IncidentType.CREW,
            title="Needs assistance",
            description="Canonical vessel-linked incident",
            status=IncidentStatus.ACTIVE,
            reporter_id="HP-NOT-IN-CURRENT-MANIFEST",
            vessel_id=vessel.id,
            agency_id=agent_profile.id,
            context_resolution="vessel_id",
        ))
        self.db.flush()

        result = self.dashboard(agent)

        self.assertEqual(result.stats.open_incidents, 1)
        self.assertEqual(result.active_vessels[0].incidents_count, 1)


if __name__ == "__main__":
    unittest.main()


class CrewAshoreDefinitionTests(unittest.TestCase):
    """A pass that was issued but never used is not a crew member on the quay.

    The live tile read 112 because it counted every shore pass without an
    in_time, including pending ones nobody signed out on, so the number only
    ever grew.
    """

    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make(self, out, back):
        agent_user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([agent_user, crew_user])
        self.db.flush()
        hpid = _uniq("HP")
        crew = CrewProfile(user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                           nationality="IN", hpid=hpid)
        vessel = Vessel(agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([crew, vessel])
        self.db.flush()
        self.db.add(VesselCrew(vessel_id=vessel.id, name="Crew", rank="able_seaman", hp_id=hpid))
        self.db.add(ShorePass(crew_profile_id=crew.id, shore_pass_id=_uniq("SP"),
                              out_time=out, in_time=back))
        self.db.flush()
        return SimpleNamespace(id=agent_user.id, role="agent",
                               agent_profile=SimpleNamespace(assigned_port=None))

    def ashore_for(self, agent):
        return get_dashboard_data(db=self.db, current_user=agent).stats.crew_in_shore

    def test_an_unused_pass_is_not_counted(self):
        agent = self.make(out=None, back=None)

        self.assertEqual(self.ashore_for(agent), 0)

    def test_signed_out_and_not_back_is_counted(self):
        agent = self.make(out=datetime.utcnow() - timedelta(hours=3), back=None)

        self.assertEqual(self.ashore_for(agent), 1)

    def test_returned_crew_is_not_counted(self):
        agent = self.make(out=datetime.utcnow() - timedelta(hours=3),
                          back=datetime.utcnow() - timedelta(minutes=20))

        self.assertEqual(self.ashore_for(agent), 0)
