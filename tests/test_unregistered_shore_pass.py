"""A shore pass for someone who has never registered — Q1, HPID spec §7, §16.

    "An agent can upload the manifest and mark a crew member eligible without
    that person having an account."

The schema said otherwise: `shore_passes.crew_profile_id` was non-null, so no
such pass could be written at all, and the issuer returned early whenever the
profile was missing. The card told the crew member they were eligible and
nothing backed it. Audit R10.

A pass is against the *assignment* — one person on one call. The profile is
the link to an account, which may arrive later or never.
"""

from datetime import datetime, timedelta, timezone
import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.api.v1.routes_vessels import _ensure_crew_shore_pass
from app.db.models.agent_profile import AgentProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import assignment_for_manifest

NOW = datetime.now(timezone.utc)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class UnregisteredShorePassTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent_user = User(email=_uniq("agent") + "@x.com", hashed_password="x",
                          role="agent")
        self.db.add(agent_user)
        self.db.flush()
        self.agency_name = _uniq("Praveen Shipping")
        self.db.add(AgentProfile(
            user_id=agent_user.id, agency_name=self.agency_name,
            location="Port", assigned_port="port_test"))
        self.vessel = Vessel(
            agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
            agency_name=self.agency_name,
            eta=NOW - timedelta(days=1), etd=NOW + timedelta(days=2))
        self.db.add(self.vessel)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def _assignment_without_an_account(self):
        """A manifest row for someone who has never registered."""
        manifest = VesselCrew(
            vessel_id=self.vessel.id, name="Unregistered Crew", rank="oiler",
            hp_id=_uniq("HP"), passport_number=_uniq("P").replace("-", "")[:12],
            nationality="IN", shore_pass_eligible=True,
        )
        self.db.add(manifest)
        self.db.flush()
        assignment = assignment_for_manifest(self.db, self.vessel, manifest)
        self.assertIsNone(
            assignment.crew_profile_id, "premise: nobody has registered")
        return assignment

    def test_a_pass_is_issued_without_an_account(self):
        assignment = self._assignment_without_an_account()
        _ensure_crew_shore_pass(
            self.db, vessel=self.vessel, assignment=assignment, profile=None,
            port="port_test", agency_name=self.agency_name)
        self.db.flush()
        issued = self.db.query(ShorePass).filter(
            ShorePass.crew_assignment_id == assignment.id).one()
        self.assertIsNone(issued.crew_profile_id)
        self.assertEqual(issued.vessel_call_id, assignment.vessel_call_id)

    def test_issuing_twice_for_one_assignment_makes_one_pass(self):
        assignment = self._assignment_without_an_account()
        for _ in range(2):
            _ensure_crew_shore_pass(
                self.db, vessel=self.vessel, assignment=assignment, profile=None,
                port="port_test", agency_name=self.agency_name)
            self.db.flush()
        self.assertEqual(
            self.db.query(ShorePass).filter(
                ShorePass.crew_assignment_id == assignment.id).count(), 1)

    def test_an_unpartnered_agency_still_issues_nothing(self):
        """The other guard is unchanged: no partner agency, no pass."""
        assignment = self._assignment_without_an_account()
        _ensure_crew_shore_pass(
            self.db, vessel=self.vessel, assignment=assignment, profile=None,
            port="port_test", agency_name="Other")
        self.db.flush()
        self.assertEqual(
            self.db.query(ShorePass).filter(
                ShorePass.crew_assignment_id == assignment.id).count(), 0)
