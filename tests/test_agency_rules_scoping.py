"""One agency's crew rules must not become every agency's.

`port_rules` holds a single row per port, so an agent saving rules wrote into
the record shared by every agency berthed there — replacing the guidance all of
them were showing their crew. Agent-authored rules now live on the agent
profile and reach only the vessels that agent manages.

Runs against the configured database inside a transaction that is always
rolled back.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.api.v1.routes_ports import (
    PortRulesIn,
    RuleItem,
    get_port_rules,
    update_port_rules,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_profile import CrewProfile
from app.db.models.port_rule import PortRule
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import assignment_for_manifest

PORT = "port_rules_scope_test"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class AgencyRulesScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.db.add(PortRule(
            port_name=PORT,
            rules=[{"title": "Port gate", "description": "Closes at 18:00",
                    "icon_type": "time"}],
        ))
        self.db.flush()

        self.agent_a, self.vessel_a, self.profile_a = self._agency()
        self.agent_b, self.vessel_b, self.profile_b = self._agency()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def _agency(self):
        user = User(email=_uniq("agent") + "@example.com",
                    hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        profile = AgentProfile(
            user_id=user.id, agency_name=_uniq("Agency"),
            location="Somewhere", assigned_port=PORT,
        )
        vessel = Vessel(
            agent_id=user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add_all([profile, vessel])
        self.db.flush()
        actor = SimpleNamespace(id=user.id, role="agent", agent_profile=profile)
        return actor, vessel, profile

    def _crew_on(self, vessel):
        user = User(email=_uniq("crew") + "@example.com",
                    hashed_password="x", role="crew")
        self.db.add(user)
        self.db.flush()
        hpid = _uniq("HP")
        # Passport and nationality are the matching pair (HPID spec §4.1).
        passport = _uniq("P")
        profile = CrewProfile(
            user_id=user.id,
            full_name="Crew",
            rank="able_seaman",
            nationality="IN",
            hpid=hpid,
            current_port=PORT,
            vessel=vessel.name,
            passport_number=passport,
        )
        manifest = VesselCrew(
            vessel_id=vessel.id,
            name="Crew",
            rank="able_seaman",
            hp_id=hpid,
            passport_number=passport,
            nationality="IN",
        )
        self.db.add_all([profile, manifest])
        self.db.flush()
        assignment_for_manifest(self.db, vessel, manifest, profile=profile)
        return SimpleNamespace(id=user.id, role="crew")

    def _save_rules(self, agent, title):
        return update_port_rules(
            port_name=PORT,
            body=PortRulesIn(rules=[RuleItem(
                title=title, description=f"{title} description", icon_type="policy",
            )]),
            db=self.db, current_user=agent,
        )

    def _titles_for(self, viewer):
        payload = get_port_rules(port_name=PORT, db=self.db, viewer=viewer)
        return [rule["title"] if isinstance(rule, dict) else rule.title
                for rule in payload["rules"]]

    # --- the reported defect ------------------------------------------------

    def test_one_agencys_rules_do_not_reach_another_agencys_crew(self):
        self._save_rules(self.agent_a, "Agency A muster")

        crew_a = self._crew_on(self.vessel_a)
        crew_b = self._crew_on(self.vessel_b)

        self.assertIn("Agency A muster", self._titles_for(crew_a))
        self.assertNotIn("Agency A muster", self._titles_for(crew_b))

    def test_saving_rules_does_not_touch_the_shared_port_row(self):
        self._save_rules(self.agent_a, "Agency A muster")

        port_row = self.db.query(PortRule).filter(PortRule.port_name == PORT).first()
        titles = [rule["title"] for rule in port_row.rules]

        self.assertEqual(titles, ["Port gate"])

    def test_one_agency_cannot_overwrite_anothers_rules(self):
        self._save_rules(self.agent_a, "Agency A muster")
        self._save_rules(self.agent_b, "Agency B muster")

        self.assertEqual(self.profile_a.agency_rules[0]["title"], "Agency A muster")
        self.assertEqual(self.profile_b.agency_rules[0]["title"], "Agency B muster")

    # --- what crew and everyone else actually see ---------------------------

    def test_crew_see_the_ports_rules_as_well_as_their_agencys(self):
        self._save_rules(self.agent_a, "Agency A muster")

        titles = self._titles_for(self._crew_on(self.vessel_a))

        self.assertEqual(titles, ["Port gate", "Agency A muster"])

    def test_an_agent_sees_their_own_rules_back(self):
        """Echoing the port row would make saved rules look like they vanished."""
        saved = self._save_rules(self.agent_a, "Agency A muster")

        self.assertEqual([r["title"] for r in saved["rules"]], ["Agency A muster"])
        self.assertIn("Agency A muster", self._titles_for(self.agent_a))

    def test_an_agents_editor_does_not_load_the_ports_rules(self):
        """This response is what the editor pre-fills, then saves back.

        Including the port's rules would let an agent's next save adopt the
        superadmin's wording as their agency's own.
        """
        self._save_rules(self.agent_a, "Agency A muster")

        payload = get_port_rules(port_name=PORT, db=self.db, viewer=self.agent_a)

        self.assertEqual(
            [rule["title"] for rule in payload["rules"]],
            ["Agency A muster"],
        )
        self.assertEqual(
            [rule["title"] for rule in payload["port_rules"]],
            ["Port gate"],
        )

    def test_contact_only_save_still_returns_the_agencys_rules(self):
        self._save_rules(self.agent_a, "Agency A muster")

        payload = update_port_rules(
            port_name=PORT,
            body=PortRulesIn(helpline_number="+91 90000 11111"),
            db=self.db,
            current_user=self.agent_a,
        )

        self.assertEqual(
            [rule["title"] for rule in payload["rules"]],
            ["Agency A muster"],
        )
        self.assertEqual(
            [rule["title"] for rule in payload["port_rules"]],
            ["Port gate"],
        )

    def test_an_agent_who_has_saved_nothing_sees_an_empty_editor(self):
        self.assertEqual(self._titles_for(self.agent_b), [])

    def test_an_anonymous_reader_sees_only_the_ports_rules(self):
        self._save_rules(self.agent_a, "Agency A muster")

        self.assertEqual(self._titles_for(None), ["Port gate"])


if __name__ == "__main__":
    unittest.main()
