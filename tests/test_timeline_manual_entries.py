"""An agent can add their own updates to a timeline; the system's own are fixed.

The design calls for "system timings and custom timings" on one timeline. The
automatic rows are the record's audit trail — if an agent could edit or delete
them, the timeline stops being evidence of what happened. So manual rows are
editable and system rows are not, and a system row is reported as *not found*
rather than *forbidden*, so it is indistinguishable from a row belonging to
another agency.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_incidents import (
    TimelineEntryIn,
    add_incident_timeline_entry,
    delete_incident_timeline_entry,
    edit_incident_timeline_entry,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.incident import (
    Incident,
    IncidentStatus,
    IncidentTimelineEvent,
    IncidentType,
)
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.session import engine
from tests.context_fixtures import immutable_call


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ManualTimelineEntryTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)
        self.agent, self.incident = self.make_agency_with_incident()
        self.other_agent, self.other_incident = self.make_agency_with_incident()

        self.system_event = IncidentTimelineEvent(
            incident_id=self.incident.id, source="system",
            event_type="reported", label="Incident Reported by Crew",
        )
        self.db.add(self.system_event)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agency_with_incident(self):
        agent_user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(agent_user)
        self.db.flush()
        agent_profile = AgentProfile(
            user_id=agent_user.id,
            agency_name=_uniq("Agency"),
            location="Test Port",
        )
        self.db.add(agent_profile)
        self.db.flush()
        vessel = Vessel(agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add(vessel)
        self.db.flush()
        incident = Incident(
            incident_id=_uniq("INC"),
            type=IncidentType.CREW,
            title="Abuse reported",
            description="Crew reported misbehaviour.",
            status=IncidentStatus.ACTIVE,
            vessel_id=vessel.id,
            agency_id=agent_profile.id,
            vessel_call_id=immutable_call(self.db, vessel, agent_profile.id).id,
            context_resolution="CONFIRMED_AGENCY",
        )
        self.db.add(incident)
        self.db.flush()
        agent = SimpleNamespace(
            id=agent_user.id, role="agent", name="Agent Desk",
            agent_profile=agent_profile,
        )
        return agent, incident

    def add(self, label="Driver Contacted", event_type="update", agent=None, incident=None):
        return add_incident_timeline_entry(
            incident_id=(incident or self.incident).id,
            body=TimelineEntryIn(label=label, event_type=event_type),
            db=self.db, current_user=agent or self.agent,
        )

    def test_an_agent_can_add_an_update(self):
        result = self.add()

        self.assertEqual(result["source"], "agent")
        self.assertTrue(result["editable"])
        self.assertEqual(result["actor_name"], "Agent Desk")

    def test_the_four_update_types_are_accepted(self):
        for kind in ("update", "investigation", "resolved", "note"):
            self.assertEqual(self.add(event_type=kind)["event_type"], kind)

    def test_an_unknown_update_type_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self.add(event_type="banana")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_an_empty_label_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self.add(label="   ")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_an_agent_can_edit_and_delete_their_own_update(self):
        created = self.add()

        edited = edit_incident_timeline_entry(
            event_id=created["id"], body=TimelineEntryIn(label="Driver Contacted Again"),
            db=self.db, current_user=self.agent,
        )
        self.assertEqual(edited["label"], "Driver Contacted Again")

        removed = delete_incident_timeline_entry(
            event_id=created["id"], db=self.db, current_user=self.agent,
        )
        self.assertTrue(removed["deleted"])

    def test_a_system_row_cannot_be_edited(self):
        """The audit trail is not the agent's to rewrite."""
        with self.assertRaises(HTTPException) as ctx:
            edit_incident_timeline_entry(
                event_id=self.system_event.id, body=TimelineEntryIn(label="tampered"),
                db=self.db, current_user=self.agent,
            )

        self.assertEqual(ctx.exception.status_code, 404)
        self.db.refresh(self.system_event)
        self.assertEqual(self.system_event.label, "Incident Reported by Crew")

    def test_a_system_row_cannot_be_deleted(self):
        with self.assertRaises(HTTPException) as ctx:
            delete_incident_timeline_entry(
                event_id=self.system_event.id, db=self.db, current_user=self.agent,
            )

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsNotNone(
            self.db.query(IncidentTimelineEvent)
            .filter(IncidentTimelineEvent.id == self.system_event.id).first()
        )

    def test_an_agent_cannot_touch_another_agencys_timeline(self):
        theirs = self.add(agent=self.other_agent, incident=self.other_incident)

        with self.assertRaises(HTTPException) as edit_denied:
            edit_incident_timeline_entry(
                event_id=theirs["id"], body=TimelineEntryIn(label="tampered"),
                db=self.db, current_user=self.agent,
            )
        with self.assertRaises(HTTPException) as delete_denied:
            delete_incident_timeline_entry(
                event_id=theirs["id"], db=self.db, current_user=self.agent,
            )

        self.assertEqual(edit_denied.exception.status_code, 404)
        self.assertEqual(delete_denied.exception.status_code, 404)

    def test_an_agent_cannot_add_to_another_agencys_incident(self):
        with self.assertRaises(HTTPException) as ctx:
            self.add(incident=self.other_incident)

        self.assertEqual(ctx.exception.status_code, 404)

    def test_denied_and_missing_are_indistinguishable(self):
        theirs = self.add(agent=self.other_agent, incident=self.other_incident)

        with self.assertRaises(HTTPException) as denied:
            delete_incident_timeline_entry(event_id=theirs["id"], db=self.db, current_user=self.agent)
        with self.assertRaises(HTTPException) as missing:
            delete_incident_timeline_entry(event_id=10**9, db=self.db, current_user=self.agent)

        self.assertEqual(denied.exception.status_code, missing.exception.status_code)
        self.assertEqual(denied.exception.detail, missing.exception.detail)

    def test_superadmin_can_add_a_manual_update(self):
        superadmin = SimpleNamespace(id=self.agent.id, role="superadmin", name="SA")

        result = self.add(agent=superadmin)
        self.assertEqual(result["source"], "superadmin")
        self.assertTrue(result["editable"])

        with self.assertRaises(HTTPException) as edit_denied:
            edit_incident_timeline_entry(
                event_id=result["id"], body=TimelineEntryIn(label="Agent rewrite"),
                db=self.db, current_user=self.agent,
            )
        with self.assertRaises(HTTPException) as delete_denied:
            delete_incident_timeline_entry(
                event_id=result["id"], db=self.db, current_user=self.agent,
            )
        self.assertEqual(edit_denied.exception.status_code, 404)
        self.assertEqual(delete_denied.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
