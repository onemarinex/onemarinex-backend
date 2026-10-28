"""The Incident Details screen reads one endpoint, so that endpoint carries the
ownership check for the incident, its timeline, its notes and the reporter.

An agent is responsible for the crew on their own ships. Asking for another
agency's incident by id must be indistinguishable from asking for one that does
not exist, so ids cannot be probed.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_incidents import (
    IncidentResponse,
    StatusUpdate,
    agent_safety_report_records,
    agent_safety_report,
    agent_incident_detail,
    update_incident_status,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_sos import CrewSos
from app.db.models.incident import (
    Incident,
    IncidentNote,
    IncidentStatus,
    IncidentTimelineEvent,
    IncidentType,
)
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from tests.context_fixtures import immutable_call


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class IncidentDetailScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)
        self.agent_a, self.incident_a = self.make_agency_with_incident()
        self.agent_b, self.incident_b = self.make_agency_with_incident()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agency_with_incident(self, resolved=True):
        agent_user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x",
                         role="crew", mobile_number="+91 90000 00000")
        self.db.add_all([agent_user, crew_user])
        self.db.flush()
        agent_profile = AgentProfile(
            user_id=agent_user.id,
            agency_name=_uniq("Agency"),
            location="Test Port",
        )
        self.db.add(agent_profile)
        self.db.flush()

        hpid = _uniq("HP")
        crew = CrewProfile(user_id=crew_user.id, full_name="Rahul Menon", rank="able_seaman",
                           nationality="IN", hpid=hpid)
        vessel = Vessel(agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([crew, vessel])
        self.db.flush()
        self.db.add(VesselCrew(vessel_id=vessel.id, name="Rahul Menon",
                               rank="able_seaman", hp_id=hpid))

        created = datetime.utcnow() - timedelta(hours=3)
        incident = Incident(
            incident_id=_uniq("INC"),
            type=IncidentType.CREW,
            title="Lost wallet",
            description="Wallet left in the cab.",
            status=IncidentStatus.RESOLVED if resolved else IncidentStatus.ACTIVE,
            category="general_support",
            sub_category="lost_property",
            severity="low",
            reporter_name="Rahul Menon",
            reporter_id=hpid,
            vessel_id=vessel.id,
            agency_id=agent_profile.id,
            vessel_call_id=immutable_call(self.db, vessel, agent_profile.id).id,
            context_resolution="CONFIRMED_AGENCY",
            created_at=created,
            resolved_at=created + timedelta(hours=1) if resolved else None,
        )
        self.db.add(incident)
        self.db.flush()

        self.db.add(IncidentTimelineEvent(
            incident_id=incident.id, source="system", event_type="reported",
            label="Incident Reported", event_time=created,
        ))
        self.db.add(IncidentNote(incident_id=incident.id, note="Spoke to the driver.",
                                 author_name="Agent"))
        self.db.flush()

        agent = SimpleNamespace(
            id=agent_user.id, role="agent", agent_profile=agent_profile
        )
        return agent, incident

    def test_agent_sees_their_own_incident_in_full(self):
        result = agent_incident_detail(
            incident_id=self.incident_a.id, db=self.db, current_user=self.agent_a,
        )

        self.assertEqual(result["incident"]["incident_id"], self.incident_a.incident_id)
        self.assertEqual(result["incident"]["category_label"], "General Support")
        self.assertEqual(result["incident"]["sub_category_label"], "Lost property")
        self.assertEqual(result["reporter"]["full_name"], "Rahul Menon")
        self.assertEqual(result["reporter"]["phone"], "+91 90000 00000")
        self.assertEqual(len(result["timeline"]), 1)
        self.assertEqual(len(result["notes"]), 1)

    def test_resolution_time_is_reported(self):
        result = agent_incident_detail(
            incident_id=self.incident_a.id, db=self.db, current_user=self.agent_a,
        )

        self.assertEqual(result["incident"]["resolution_seconds"], 3600)

    def test_open_incident_has_no_resolution_time(self):
        agent, incident = self.make_agency_with_incident(resolved=False)

        result = agent_incident_detail(
            incident_id=incident.id, db=self.db, current_user=agent,
        )

        self.assertIsNone(result["incident"]["resolution_seconds"])

    def test_close_stamped_before_the_report_is_not_reported_as_negative(self):
        """Bad data should show as absent, not as a negative duration."""
        self.incident_a.resolved_at = self.incident_a.created_at - timedelta(minutes=20)
        self.db.flush()

        result = agent_incident_detail(
            incident_id=self.incident_a.id, db=self.db, current_user=self.agent_a,
        )

        self.assertIsNone(result["incident"]["resolution_seconds"])

    def test_agent_cannot_open_another_agencys_incident(self):
        with self.assertRaises(HTTPException) as ctx:
            agent_incident_detail(
                incident_id=self.incident_b.id, db=self.db, current_user=self.agent_a,
            )

        self.assertEqual(ctx.exception.status_code, 404)

    def test_denied_and_missing_are_indistinguishable(self):
        with self.assertRaises(HTTPException) as denied:
            agent_incident_detail(
                incident_id=self.incident_b.id, db=self.db, current_user=self.agent_a,
            )
        with self.assertRaises(HTTPException) as missing:
            agent_incident_detail(
                incident_id=10**9, db=self.db, current_user=self.agent_a,
            )

        self.assertEqual(denied.exception.status_code, missing.exception.status_code)
        self.assertEqual(denied.exception.detail, missing.exception.detail)

    def test_status_change_returns_a_valid_response(self):
        """The endpoint declares IncidentResponse; the payload must satisfy it.

        It did not: `updated_at` was missing, so every status change committed
        and then failed response validation with a 500. The change was saved
        but the client was told it had failed.
        """
        agent, incident = self.make_agency_with_incident(resolved=False)

        result = asyncio.run(
            update_incident_status(
                id=incident.id, status_update=StatusUpdate(status=IncidentStatus.INVESTIGATING),
                db=self.db, current_user=agent,
            )
        )

        IncidentResponse(**result)  # raises if a required field is missing
        self.assertEqual(result["status"], IncidentStatus.INVESTIGATING)
        self.assertIsNotNone(result["updated_at"])

    def test_terminal_incident_cannot_be_reopened(self):
        with self.assertRaises(HTTPException) as error:
            asyncio.run(
                update_incident_status(
                    id=self.incident_a.id, status_update=StatusUpdate(status=IncidentStatus.ACTIVE),
                    db=self.db, current_user=self.agent_a,
                )
            )
        self.assertEqual(error.exception.status_code, 409)

    def test_superadmin_can_open_incident_detail(self):
        superadmin = SimpleNamespace(id=self.agent_a.id, role="superadmin")

        detail = agent_incident_detail(
            incident_id=self.incident_a.id, db=self.db, current_user=superadmin,
        )
        self.assertEqual(detail["incident"]["id"], self.incident_a.id)

    def test_safety_report_lists_resolved_record_for_owned_vessel(self):
        result = agent_safety_report_records(
            vessel_id=self.incident_a.vessel_id,
            db=self.db,
            current_user=self.agent_a,
        )

        self.assertEqual(result["vessel"]["id"], self.incident_a.vessel_id)
        self.assertEqual(result["records"][0]["reference"], self.incident_a.incident_id)
        self.assertEqual(result["records"][0]["status"], "RESOLVED")
        self.assertIsNotNone(result["records"][0]["resolved_at"])

    def test_safety_report_cannot_probe_another_agents_vessel(self):
        with self.assertRaises(HTTPException) as denied:
            agent_safety_report_records(
                vessel_id=self.incident_b.vessel_id,
                db=self.db,
                current_user=self.agent_a,
            )
        with self.assertRaises(HTTPException) as missing:
            agent_safety_report_records(
                vessel_id=10**9,
                db=self.db,
                current_user=self.agent_a,
            )
        self.assertEqual(denied.exception.status_code, 404)
        self.assertEqual(denied.exception.detail, missing.exception.detail)

    def test_report_payload_has_server_stamp_and_enforces_record_ownership(self):
        own = agent_safety_report(
            record_kind="incident", record_id=self.incident_a.id,
            db=self.db, current_user=self.agent_a,
        )
        self.assertEqual(own["record_kind"], "incident")
        self.assertIsNotNone(own["generated_at"].tzinfo)
        self.assertEqual(own["payload"]["incident"]["id"], self.incident_a.id)

        with self.assertRaises(HTTPException) as denied:
            agent_safety_report(
                record_kind="incident", record_id=self.incident_b.id,
                db=self.db, current_user=self.agent_a,
            )
        self.assertEqual(denied.exception.status_code, 404)

    def test_safety_report_includes_active_sos_and_missing_trip_is_safe(self):
        manifest_hpid = self.db.query(VesselCrew.hp_id).filter(
            VesselCrew.vessel_id == self.incident_a.vessel_id
        ).scalar()
        crew = self.db.query(CrewProfile).filter(
            CrewProfile.hpid == manifest_hpid
        ).one()
        sos = CrewSos(
            user_id=crew.user_id,
            crew_profile_id=crew.id,
            vessel=self.incident_a.vessel.name,
            vessel_id=self.incident_a.vessel_id,
            agency_id=self.incident_a.agency_id,
            vessel_call_id=self.incident_a.vessel_call_id,
            context_resolution="CONFIRMED_AGENCY",
            status="ACTIVE",
            lat=17.7019,
            lng=83.2897,
        )
        self.db.add(sos)
        self.db.flush()

        report = agent_safety_report_records(
            vessel_id=self.incident_a.vessel_id,
            db=self.db,
            current_user=self.agent_a,
        )
        self.assertTrue(any(
            item["kind"] == "sos" and item["id"] == sos.id
            for item in report["records"]
        ))

        detail = agent_incident_detail(
            incident_id=self.incident_a.id,
            db=self.db,
            current_user=self.agent_a,
        )
        self.assertIsNone(detail["trip"])


if __name__ == "__main__":
    unittest.main()
