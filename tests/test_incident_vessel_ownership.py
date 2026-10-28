"""An incident belongs to a ship, not to a string.

Incidents were matched to an agent through the reporter's HPID. An HPID is
regenerated when a crew member is re-linked to a vessel or re-uploaded on a
manifest, and every incident raised under the old HPID then vanished from the
agent's list while the crew member was still aboard. `incidents.vessel_id` is
stamped once at creation and does not move, so ownership follows it.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.api.v1.routes_incidents import agent_incident_list
from app.db.models.agent_profile import AgentProfile
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from tests.context_fixtures import immutable_call


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class IncidentVesselOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent_user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(agent_user)
        self.db.flush()
        self.agent_profile = AgentProfile(
            user_id=agent_user.id,
            agency_name=_uniq("Agency"),
            location="Test Port",
        )
        self.db.add(self.agent_profile)
        self.db.flush()

        self.vessel = Vessel(agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                             vessel_type="Bulk Carrier", status="Active")
        self.db.add(self.vessel)
        self.db.flush()

        self.hpid = _uniq("HP")
        self.crew_row = VesselCrew(vessel_id=self.vessel.id, name="Yogesh Kothavale",
                                   rank="chief_officer", hp_id=self.hpid)
        self.db.add(self.crew_row)

        self.incident = Incident(
            incident_id=_uniq("INC"),
            type=IncidentType.CREW,
            title="Late pickup",
            description="Cab was late.",
            status=IncidentStatus.ACTIVE,
            category="driver_vehicle",
            sub_category="late_pickup",
            severity="medium",
            reporter_name="Yogesh Kothavale",
            reporter_id=self.hpid,
            vessel_id=self.vessel.id,
            agency_id=self.agent_profile.id,
            vessel_call_id=immutable_call(
                self.db, self.vessel, self.agent_profile.id
            ).id,
            context_resolution="vessel_id",
        )
        self.db.add(self.incident)
        self.db.flush()

        self.agent = SimpleNamespace(id=agent_user.id, role="agent")

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def listed_ids(self):
        return [i["incident_id"] for i in
                agent_incident_list(db=self.db, current_user=self.agent)["incidents"]]

    def test_incident_is_listed_normally(self):
        self.assertIn(self.incident.incident_id, self.listed_ids())

    def test_incident_survives_the_crews_hpid_being_regenerated(self):
        """The regression: re-linking a crew member reissues their HPID."""
        self.crew_row.hp_id = _uniq("HP-NEW")
        self.db.flush()

        self.assertIn(self.incident.incident_id, self.listed_ids())

    def test_incident_survives_the_crew_leaving_the_manifest(self):
        self.db.delete(self.crew_row)
        self.db.flush()

        self.assertIn(self.incident.incident_id, self.listed_ids())

    def test_unresolved_legacy_incident_is_not_inferred_from_current_hpid(self):
        """Unresolved history must not follow a crew member to a new vessel."""
        self.incident.vessel_id = None
        self.incident.agency_id = None
        self.incident.context_resolution = "unresolved"
        self.db.flush()

        self.assertNotIn(self.incident.incident_id, self.listed_ids())

    def test_another_agencys_incident_is_not_listed(self):
        other_user = User(email=_uniq("other") + "@example.com", hashed_password="x", role="agent")
        self.db.add(other_user)
        self.db.flush()
        other_profile = AgentProfile(
            user_id=other_user.id, agency_name=_uniq("Agency"), location="Elsewhere"
        )
        self.db.add(other_profile)
        self.db.flush()
        other_vessel = Vessel(agent_id=other_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                              vessel_type="Tanker", status="Active")
        self.db.add(other_vessel)
        self.db.flush()
        theirs = Incident(
            incident_id=_uniq("INC"), type=IncidentType.CREW,
            title="Theirs", description="Not ours.", status=IncidentStatus.ACTIVE,
            reporter_id=_uniq("HP"), vessel_id=other_vessel.id,
            agency_id=other_profile.id, context_resolution="vessel_id",
        )
        self.db.add(theirs)
        self.db.flush()

        self.assertNotIn(theirs.incident_id, self.listed_ids())


if __name__ == "__main__":
    unittest.main()
