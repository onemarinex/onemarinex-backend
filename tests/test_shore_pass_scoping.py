"""Shore passes belong to the agent whose ship the crew member sails on.

The list was scoped by port, so every agency berthed alongside saw each other's
requests. Worse, **approve and reject had no ownership check at all** — any
agent could grant or refuse shore leave for any crew member in the system by
passing an id.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_agents import (
    ShorePassActionIn,
    approve_shore_pass,
    get_shore_pass_requests,
    reject_shore_pass,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel_call import VesselCall
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ShorePassScopingTests(unittest.TestCase):
    PORT = "port_shared_harbour"

    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)
        self.agent_a, self.pass_a = self.make_agency_with_request()
        self.agent_b, self.pass_b = self.make_agency_with_request()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agency_with_request(self):
        agent_user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([agent_user, crew_user])
        self.db.flush()

        hpid = _uniq("HP")
        crew = CrewProfile(user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                           nationality="IN", hpid=hpid, current_port=self.PORT)
        vessel = Vessel(agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([crew, vessel])
        self.db.flush()
        agency = AgentProfile(user_id=agent_user.id, agency_name=_uniq("Agency"), location="Port", assigned_port=self.PORT)
        self.db.add(agency); self.db.flush()
        manifest = VesselCrew(vessel_id=vessel.id, name="Crew", rank="able_seaman", hp_id=hpid,
            shore_pass_eligible=True, shore_pass_valid_upto=datetime.now(timezone.utc)+timedelta(days=1))
        call = VesselCall(vessel_id=vessel.id, agency_id=agency.id, vessel_name=vessel.name,
            agency_name=agency.agency_name, started_at=datetime.now(timezone.utc)-timedelta(hours=1))
        self.db.add_all([manifest, call]); self.db.flush()
        assignment = CrewAssignment(vessel_call_id=call.id, crew_profile_id=crew.id,
            vessel_crew_id=manifest.id, crew_name="Crew", shore_pass_eligible=True,
            started_at=call.started_at)
        self.db.add(assignment); self.db.flush()

        sp = ShorePass(crew_profile_id=crew.id, crew_assignment_id=assignment.id,
                       vessel_call_id=call.id, shore_pass_id=_uniq("SP"),
                       port_name=self.PORT, vessel_name=vessel.name, status="pending")
        self.db.add(sp)
        self.db.flush()

        agent = SimpleNamespace(
            id=agent_user.id, role="agent",
            agent_profile=SimpleNamespace(assigned_port=self.PORT),
        )
        return agent, sp

    def test_agent_lists_only_their_own_crews_requests(self):
        listed = get_shore_pass_requests(db=self.db, current_user=self.agent_a)

        self.assertEqual([sp.id for sp in listed], [self.pass_a.id])

    def test_agent_cannot_approve_another_agencys_shore_pass(self):
        """The serious one: granting shore leave for crew you are not responsible for."""
        with self.assertRaises(HTTPException) as ctx:
            approve_shore_pass(request_id=self.pass_b.id, db=self.db, current_user=self.agent_a)

        self.assertEqual(ctx.exception.status_code, 404)
        self.db.refresh(self.pass_b)
        self.assertEqual(self.pass_b.status, "pending")
        self.assertNotEqual(self.pass_b.approved_by_id, self.agent_a.id)

    def test_agent_cannot_reject_another_agencys_shore_pass(self):
        with self.assertRaises(HTTPException) as ctx:
            reject_shore_pass(
                request_id=self.pass_b.id,
                body=ShorePassActionIn(rejection_reason="no"),
                db=self.db, current_user=self.agent_a,
            )

        self.assertEqual(ctx.exception.status_code, 404)
        self.db.refresh(self.pass_b)
        self.assertEqual(self.pass_b.status, "pending")

    def test_denied_and_missing_are_indistinguishable(self):
        with self.assertRaises(HTTPException) as denied:
            approve_shore_pass(request_id=self.pass_b.id, db=self.db, current_user=self.agent_a)
        with self.assertRaises(HTTPException) as missing:
            approve_shore_pass(request_id=10**9, db=self.db, current_user=self.agent_a)

        self.assertEqual(denied.exception.status_code, missing.exception.status_code)
        self.assertEqual(denied.exception.detail, missing.exception.detail)

    def test_agent_can_still_approve_their_own(self):
        result = approve_shore_pass(request_id=self.pass_a.id, db=self.db, current_user=self.agent_a)

        self.assertEqual(result.status, "approved")
        self.assertTrue(result.is_verified)


if __name__ == "__main__":
    unittest.main()
