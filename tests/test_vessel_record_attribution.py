"""A crew member's history stays with the ship it happened on.

The reported defect: a crew member registered a passport under one vessel,
raised an SOS there, then joined a second vessel. Every SOS and incident from
the first ship then appeared under the second.

The cause is that both manifests legitimately match the same person — they
really were crew on both — so asking "who is on this vessel" and taking all
their records drags the whole history onto whichever ship they joined last.
A CrewSos stamps the vessel it was raised on; that stamp is what decides.

Runs against the configured database inside a transaction that is always
rolled back.
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_sos import CrewSos
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from tests.context_fixtures import immutable_call


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class VesselAttributionTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent_user = User(email=_uniq("agent") + "@example.com",
                          hashed_password="x", role="agent")
        self.db.add(agent_user)
        self.db.flush()
        self.agent_profile = AgentProfile(
            user_id=agent_user.id,
            agency_name="Test Agency",
            location="Test Port",
        )
        self.db.add(self.agent_profile)
        self.db.flush()
        self.agent_user = agent_user
        self.agent = SimpleNamespace(
            id=agent_user.id, role="agent",
            agent_profile=self.agent_profile,
        )

        # Two ships under one agent, and one crew member who sails on both.
        self.old_ship = self._vessel("MV BABYLON")
        self.new_ship = self._vessel("MV JIM MING 82")

        self.passport = _uniq("P").replace("-", "")[:12].upper()
        crew_user = User(email=_uniq("crew") + "@example.com",
                         hashed_password="x", role="crew")
        self.db.add(crew_user)
        self.db.flush()
        self.crew = CrewProfile(
            user_id=crew_user.id, full_name="Roaming Crew", rank="able_seaman",
            nationality="IN", passport_number=self.passport, hpid=_uniq("HP"),
            # The profile now names the new ship, as it would after transferring.
            vessel="MV JIM MING 82",
        )
        self.db.add(self.crew)
        self.db.flush()

        # The same passport sits on both manifests.
        for vessel in (self.old_ship, self.new_ship):
            self.db.add(VesselCrew(
                vessel_id=vessel.id, name="Roaming Crew", rank="able_seaman",
                passport_number=self.passport, shore_pass_eligible=True,
            ))
        self.db.flush()

    def _vessel(self, name):
        vessel = Vessel(
            agent_id=self.agent_user.id, name=name, imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(vessel)
        self.db.flush()
        return vessel

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def _sos(self, vessel):
        sos = CrewSos(
            user_id=self.crew.user_id,
            crew_profile_id=self.crew.id,
            crew_email="crew@example.com",
            sos_email="ship@example.com",
            port_name="port_test",
            vessel=vessel.name if vessel else None,
            vessel_id=vessel.id if vessel else None,
            agency_id=self.agent_profile.id if vessel else None,
            vessel_call_id=(
                immutable_call(self.db, vessel, self.agent_profile.id).id
                if vessel
                else None
            ),
            context_resolution="vessel_id" if vessel else "unresolved",
            status="ACTIVE",
        )
        self.db.add(sos)
        self.db.flush()
        return sos

    def _reports_for(self, vessel):
        from app.api.v1.routes_incidents import agent_safety_report_records
        payload = agent_safety_report_records(
            vessel_id=vessel.id, db=self.db, current_user=self.agent)
        return payload["records"]

    def _list_for(self, vessel):
        from app.api.v1.routes_incidents import agent_incident_list
        # The vessel page shows one combined safety list, so it opts in.
        payload = agent_incident_list(
            status_filter=None, vessel_id=vessel.id, include_sos=True,
            db=self.db, current_user=self.agent)
        return payload["incidents"]

    # --- the reported defect ------------------------------------------------

    def test_an_sos_stays_on_the_ship_it_was_raised_on(self):
        old_sos = self._sos(self.old_ship)

        old_refs = [r["reference"] for r in self._reports_for(self.old_ship)]
        new_refs = [r["reference"] for r in self._reports_for(self.new_ship)]

        self.assertIn(f"SOS-{old_sos.id}", old_refs)
        self.assertNotIn(f"SOS-{old_sos.id}", new_refs)

    def test_the_same_holds_for_the_vessel_incident_list(self):
        old_sos = self._sos(self.old_ship)
        new_sos = self._sos(self.new_ship)

        old_refs = [r["incident_id"] for r in self._list_for(self.old_ship)]
        new_refs = [r["incident_id"] for r in self._list_for(self.new_ship)]

        self.assertEqual(old_refs, [f"SOS-{old_sos.id}"])
        self.assertEqual(new_refs, [f"SOS-{new_sos.id}"])

    def test_incident_management_does_not_repeat_sos_alerts(self):
        """It has a dedicated SOS view beside it.

        Returning SOS here unconditionally put every emergency in both places
        at once — the duplication that removing the mirrored Incident row was
        meant to end.
        """
        from app.api.v1.routes_incidents import agent_incident_list

        self._sos(self.new_ship)

        payload = agent_incident_list(
            status_filter=None, vessel_id=None,
            db=self.db, current_user=self.agent)

        self.assertEqual(payload["incidents"], [])

    def test_display_name_cannot_override_the_immutable_vessel_id(self):
        sos = self._sos(self.new_ship)
        sos.vessel = "MV BABYLON"
        self.db.flush()

        refs = [r["reference"] for r in self._reports_for(self.new_ship)]

        self.assertIn(f"SOS-{sos.id}", refs)

    def test_an_unresolved_sos_is_not_inferred_into_an_agent_report(self):
        legacy = self._sos(None)

        refs = [r["reference"] for r in self._reports_for(self.new_ship)]

        self.assertNotIn(f"SOS-{legacy.id}", refs)

    # --- guarding the over-reach that caused it -----------------------------

    def test_manifest_linkage_ignores_profiles_that_are_not_on_the_crew_list(self):
        """Naming a ship in your profile does not put you on its manifest."""
        from app.services.crew_linkage import vessel_crew_profiles

        stowaway_user = User(email=_uniq("crew") + "@example.com",
                             hashed_password="x", role="crew")
        self.db.add(stowaway_user)
        self.db.flush()
        self.db.add(CrewProfile(
            user_id=stowaway_user.id, full_name="Not On The List",
            rank="able_seaman", nationality="IN", hpid=_uniq("HP"),
            passport_number=_uniq("Q").replace("-", "")[:12].upper(),
            vessel="MV JIM MING 82",
        ))
        self.db.flush()

        matched = vessel_crew_profiles(self.db, self.new_ship)

        self.assertEqual([p.id for p in matched], [self.crew.id])


if __name__ == "__main__":
    unittest.main()
