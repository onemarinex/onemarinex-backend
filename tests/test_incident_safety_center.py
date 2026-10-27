"""Crew Safety Center: categories are recorded, incidents reach a vessel, and
agents see only their own.

Before this, `RaiseIncident.tsx` sent a category that the API had no field for,
so Pydantic dropped it and every crew incident was stored as the hardcoded title
"Crew Reported Incident" with nothing to group or filter by. An incident also
had no link to a ship — the only route was the reporter's HPID, which fails for
crew who have left the manifest.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import asyncio
import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import routes_incidents as ri
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import CabBooking, VehicleType
from app.db.models.crew_profile import CrewProfile
from app.db.models.incident import Incident, IncidentStatus, IncidentTimelineEvent, IncidentType
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import assignment_for_manifest


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def run(coro):
    return asyncio.run(coro)


class SafetyCenterTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.agent_a, self.crew_a, self.vessel_a = self.make_agency()
        self.agent_b, self.crew_b, self.vessel_b = self.make_agency()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agency(self):
        agent_user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x",
                         role="crew", name="Test Crew")
        self.db.add_all([agent_user, crew_user])
        self.db.flush()
        agent_profile = AgentProfile(
            user_id=agent_user.id,
            agency_name=_uniq("Agency"),
            location="Test Port",
            assigned_port="port_test",
        )
        self.db.add(agent_profile)
        self.db.flush()

        hpid = _uniq("HP")
        # Passport and nationality are the matching pair (HPID spec §4.1); the
        # HPID is no longer a key.
        passport = _uniq("P")
        crew = CrewProfile(user_id=crew_user.id, full_name="Test Crew", rank="third_officer",
                           nationality="IN", hpid=hpid, current_port="port_test",
                           passport_number=passport)
        vessel = Vessel(agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([crew, vessel])
        self.db.flush()
        manifest = VesselCrew(
            vessel_id=vessel.id,
            name="Test Crew",
            rank="third_officer",
            hp_id=hpid,
            passport_number=passport,
            nationality="IN",
        )
        self.db.add(manifest)
        self.db.flush()
        assignment_for_manifest(self.db, vessel, manifest, profile=crew)

        agent = SimpleNamespace(
            id=agent_user.id, role="agent", name="Agent", agent_profile=agent_profile
        )
        crew_actor = SimpleNamespace(id=crew_user.id, role="crew", name="Test Crew")
        return agent, crew_actor, vessel

    def raise_incident(self, crew_actor, **overrides):
        payload = ri.IncidentCreate(
            type=IncidentType.CREW,
            title="Crew Reported Incident",
            description="Something happened",
            **overrides,
        )
        return run(ri.create_incident(payload, db=self.db, current_user=crew_actor))

    def make_trip(self, crew_actor, suffix="trip"):
        crew = self.db.query(CrewProfile).filter(CrewProfile.user_id == crew_actor.id).one()
        booking = CabBooking(
            booking_id=_uniq(suffix), crew_id=crew.id, port=crew.current_port,
            pickup_address="Port Gate", pickup_lat=17.7, pickup_lng=83.3,
            drop_address="City Centre", drop_lat=17.72, drop_lng=83.31,
            vehicle_type=VehicleType.AC, vehicle_name="Sedan",
            estimated_price=500, distance_km=8, num_passengers=1,
        )
        self.db.add(booking)
        self.db.flush()
        return booking

    def make_assignment_scoped_trip(self, crew_actor, suffix="scoped-trip"):
        booking = self.make_trip(crew_actor, suffix)
        crew = self.db.query(CrewProfile).filter(
            CrewProfile.user_id == crew_actor.id
        ).one()
        manifest = self.db.query(VesselCrew).filter(
            VesselCrew.hp_id == crew.hpid
        ).one()
        vessel = self.db.query(Vessel).filter(Vessel.id == manifest.vessel_id).one()
        from app.services.historical_context import (
            active_vessel_call,
            assignment_for_manifest,
        )

        call = active_vessel_call(self.db, vessel)
        assignment = assignment_for_manifest(
            self.db, vessel, manifest, profile=crew
        )
        booking.crew_assignment_id = assignment.id
        booking.vessel_call_id = call.id
        booking.vessel_id = call.vessel_id
        booking.agency_id = call.agency_id
        booking.port_id = call.port_id
        booking.context_resolution = "assignment"
        self.db.flush()
        return booking

    # -- category is actually stored -------------------------------------

    def test_category_and_sub_category_are_recorded(self):
        result = self.raise_incident(
            self.crew_a, category="payment_issue", sub_category="overcharged"
        )

        self.assertEqual(result["category"], "payment_issue")
        self.assertEqual(result["sub_category"], "overcharged")
        self.assertEqual(result["category_label"], "Payment Issue")

    def test_unknown_category_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            self.raise_incident(self.crew_a, category="not_a_category")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_sub_category_must_belong_to_its_category(self):
        with self.assertRaises(HTTPException) as ctx:
            self.raise_incident(
                self.crew_a, category="payment_issue", sub_category="harassment"
            )
        self.assertEqual(ctx.exception.status_code, 400)

    # -- severity is decided by the system, not the reporter --------------

    def test_safety_critical_categories_start_high(self):
        for category in ("medical_emergency", "safety_security"):
            with self.subTest(category):
                result = self.raise_incident(self.crew_a, category=category)
                self.assertEqual(result["severity"], "high")

    def test_crew_cannot_set_their_own_severity(self):
        """A person in trouble should not be grading their own risk."""
        result = self.raise_incident(
            self.crew_a, category="service_complaint", severity="high"
        )

        self.assertEqual(result["severity"], "low")

    # -- vessel linkage ---------------------------------------------------

    def test_incident_is_linked_to_the_reporters_vessel(self):
        result = self.raise_incident(self.crew_a, category="general_support")

        self.assertEqual(result["vessel_id"], self.vessel_a.id)
        self.assertEqual(result["vessel_name"], self.vessel_a.name)

    def test_crew_cannot_choose_an_incident_recipient(self):
        """A crafted aggregator_id must not redirect a crew incident."""
        result = self.raise_incident(
            self.crew_a, category="general_support", aggregator_id=987654321
        )

        incident = self.db.query(Incident).filter(Incident.id == result["id"]).one()
        self.assertIsNone(incident.aggregator_id)
        self.assertEqual(incident.vessel_id, self.vessel_a.id)

    def test_superadmin_can_manage_detail_without_weakening_agent_scope(self):
        created = self.raise_incident(
            self.crew_a, category="general_support"
        )
        superadmin = SimpleNamespace(
            id=999999, role="superadmin", name="Super Admin"
        )

        detail = ri.agent_incident_detail(
            created["id"], db=self.db, current_user=superadmin
        )
        self.assertEqual(detail["incident"]["id"], created["id"])
        with self.assertRaises(HTTPException) as hidden:
            ri.agent_incident_detail(
                created["id"], db=self.db, current_user=self.agent_b
            )
        self.assertEqual(hidden.exception.status_code, 404)

    def test_legacy_trip_without_assignment_context_is_rejected(self):
        booking = self.make_trip(self.crew_a)
        with self.assertRaises(HTTPException) as ctx:
            self.raise_incident(
                self.crew_a, category="general_support", trip_id=booking.booking_id
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_crew_can_explicitly_attach_own_assignment_scoped_trip(self):
        booking = self.make_assignment_scoped_trip(self.crew_a)
        result = self.raise_incident(
            self.crew_a, category="general_support", trip_id=booking.booking_id
        )
        self.assertEqual(result["trip_id"], booking.booking_id)
        incident = self.db.query(Incident).filter(Incident.id == result["id"]).one()
        self.assertEqual(incident.crew_assignment_id, booking.crew_assignment_id)

    def test_crew_cannot_attach_another_crews_trip(self):
        other_trip = self.make_trip(self.crew_b)
        with self.assertRaises(HTTPException) as ctx:
            self.raise_incident(
                self.crew_a, category="general_support", trip_id=other_trip.booking_id
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_trip_is_not_silently_inferred_when_none_is_selected(self):
        self.make_trip(self.crew_a)
        result = self.raise_incident(self.crew_a, category="general_support")
        self.assertIsNone(result["trip_id"])

    def test_eligible_trip_list_is_crew_scoped(self):
        own = self.make_trip(self.crew_a, "own")
        other = self.make_trip(self.crew_b, "other")
        result = ri.list_eligible_incident_trips(db=self.db, current_user=self.crew_a)
        ids = [item["trip_id"] for item in result["trips"]]
        self.assertIn(own.booking_id, ids)
        self.assertNotIn(other.booking_id, ids)

    def test_vessel_resolution_falls_back_to_passport(self):
        """Legacy IN/IND HPID mismatches must not leave an incident unassigned."""
        crew = self.db.query(CrewProfile).filter(
            CrewProfile.user_id == self.crew_a.id
        ).one()
        manifest = self.db.query(VesselCrew).filter(
            VesselCrew.vessel_id == self.vessel_a.id
        ).one()
        passport = _uniq("PASS")
        crew.passport_number = passport
        manifest.passport_number = passport
        manifest.hp_id = _uniq("DIFFERENT-HP")
        self.db.flush()

        result = self.raise_incident(self.crew_a, category="general_support")

        self.assertEqual(result["vessel_id"], self.vessel_a.id)

    def test_unassigned_vessel_is_retained_for_superadmin_follow_up(self):
        self.vessel_a.agent_id = None
        self.db.flush()

        with self.assertRaises(HTTPException) as ctx:
            self.raise_incident(self.crew_a, category="general_support")
        self.assertEqual(ctx.exception.status_code, 409)

    # -- timeline ---------------------------------------------------------

    def test_raising_an_incident_starts_its_timeline(self):
        result = self.raise_incident(self.crew_a, category="general_support")

        events = self.db.query(IncidentTimelineEvent).filter(
            IncidentTimelineEvent.incident_id == result["id"]
        ).all()
        self.assertEqual([e.event_type for e in events], ["reported", "received"])

    def test_status_change_is_recorded_and_stamps_resolved_at(self):
        created = self.raise_incident(self.crew_a, category="general_support")

        run(ri.update_incident_status(
            id=created["id"],
            status_update=ri.StatusUpdate(status=IncidentStatus.RESOLVED),
            db=self.db, current_user=self.agent_a,
        ))

        incident = self.db.query(Incident).filter(Incident.id == created["id"]).first()
        self.assertIsNotNone(incident.resolved_at)
        types = [e.event_type for e in self.db.query(IncidentTimelineEvent).filter(
            IncidentTimelineEvent.incident_id == created["id"]).all()]
        self.assertIn("resolved", types)

    def test_cancelling_stamps_cancelled_at(self):
        created = self.raise_incident(self.crew_a, category="general_support")

        run(ri.update_incident_status(
            id=created["id"],
            status_update=ri.StatusUpdate(status=IncidentStatus.CANCELLED),
            db=self.db, current_user=self.agent_a,
        ))

        incident = self.db.query(Incident).filter(Incident.id == created["id"]).first()
        self.assertIsNotNone(incident.cancelled_at)

    # -- scoping ----------------------------------------------------------

    def test_agent_sees_only_incidents_from_their_own_vessels(self):
        self.raise_incident(self.crew_a, category="general_support")
        self.raise_incident(self.crew_b, category="general_support")
        self.raise_incident(self.crew_b, category="payment_issue")

        mine = ri.agent_incident_list(db=self.db, current_user=self.agent_a)["incidents"]

        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["vessel_id"], self.vessel_a.id)

    def test_summary_counts_only_this_agents_incidents(self):
        self.raise_incident(self.crew_a, category="general_support")
        self.raise_incident(self.crew_b, category="general_support")

        summary = ri.agent_safety_summary(db=self.db, current_user=self.agent_a)

        self.assertEqual(summary["open_incidents"], 1)

    def test_non_agents_cannot_read_the_agent_views(self):
        for fn in (ri.agent_safety_summary, ri.agent_incident_list):
            with self.subTest(fn.__name__):
                with self.assertRaises(HTTPException) as ctx:
                    fn(db=self.db, current_user=self.crew_a)
                self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
