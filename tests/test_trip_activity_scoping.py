"""Crew real-time activity mirrors the driver's magic link, for the agent.

The magic link itself is public — anyone holding the unguessable token can see
a trip's progress. The agent-facing equivalent is addressed by booking id, which
is far more guessable, so it must be scoped to the agent's own crew.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_bookings import _require_magic_link_active
from app.api.v1.routes_trips import get_trip_activity
from app.db.models.cab_booking import BookingStatus, CabBooking, VehicleType
from app.db.models.booking_timeline import BookingTimeline, TimelineEventType
from app.db.models.crew_profile import CrewProfile
from app.db.models.driver_magic_link import DriverMagicLink, DriverMagicLinkReachEvent
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.db.models.agent_profile import AgentProfile
from tests.context_fixtures import immutable_call
from app.services.magic_link_service import mark_stop_reached
from app.services.booking_service import serialize_booking
from app.services.timeline_service import get_booking_timeline


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class TripActivityScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)
        self.agent_a, self.booking_a = self.make_agent_with_trip()
        self.agent_b, self.booking_b = self.make_agent_with_trip()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agent_with_trip(self):
        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add_all([agent, crew_user])
        self.db.flush()

        hpid = _uniq("HP")
        crew = CrewProfile(user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                           nationality="IN", hpid=hpid)
        vessel = Vessel(agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                        vessel_type="Bulk Carrier", status="Active")
        self.db.add_all([crew, vessel])
        self.db.flush()
        self.db.add(VesselCrew(vessel_id=vessel.id, name="Crew", rank="able_seaman", hp_id=hpid))

        profile = AgentProfile(
            user_id=agent.id, agency_name=_uniq("Agency"), location="Fixture"
        )
        self.db.add(profile)
        self.db.flush()
        booking = CabBooking(
            vessel_id=vessel.id,
            agency_id=profile.id,
            vessel_call_id=immutable_call(self.db, vessel, profile.id).id,
            context_resolution="CONFIRMED_AGENCY",
            booking_id=_uniq("CAB"),
            crew_id=crew.id,
            pickup_address="Gate",
            pickup_lat=0,
            pickup_lng=0,
            drop_address="City",
            drop_lat=0,
            drop_lng=0,
            vehicle_type=VehicleType.AC,
            vehicle_name="Sedan",
            estimated_price=100,
            distance_km=5,
            status=BookingStatus.ON_TRIP,
            driver_name="Ramesh",
            driver_phone="+910000000000",
            driver_plate="AP39 AB 1234",
        )
        self.db.add(booking)
        self.db.flush()

        return SimpleNamespace(id=agent.id, role="agent", agent_profile=None), booking

    def activity(self, agent, booking_id):
        return get_trip_activity(booking_id=booking_id, db=self.db, current_user=agent)

    def test_agent_sees_activity_for_their_own_trip(self):
        result = self.activity(self.agent_a, self.booking_a.booking_id)

        self.assertEqual(result.booking_id, self.booking_a.booking_id)
        self.assertEqual(result.driver_name, "Ramesh")

    def test_no_driver_link_yet_reports_tracking_unavailable(self):
        """The trip exists but nothing is being tracked — say so plainly."""
        result = self.activity(self.agent_a, self.booking_a.booking_id)

        self.assertFalse(result.tracking_available)
        self.assertEqual(result.stops_total, 0)

    def test_legacy_booking_details_show_persisted_route_without_claiming_tracking(self):
        result = serialize_booking(self.booking_a)

        self.assertFalse(result["tracking_available"])
        self.assertEqual(
            [stop["address"] for stop in result["itinerary_stops"]],
            ["Gate", "City"],
        )
        self.assertTrue(all(stop["derived_from_booking"] for stop in result["itinerary_stops"]))

    def test_legacy_booking_timeline_uses_only_exact_persisted_timestamps(self):
        started = datetime.utcnow() - timedelta(minutes=20)
        completed = datetime.utcnow() - timedelta(minutes=5)
        self.booking_a.created_at = started - timedelta(minutes=10)
        self.booking_a.status = BookingStatus.COMPLETED
        self.booking_a.trip_started_at = started
        self.booking_a.trip_completed_at = completed
        self.db.flush()

        timeline = get_booking_timeline(self.db, self.booking_a.id)

        self.assertEqual(
            [event["event_type"] for event in timeline],
            ["BOOKING_CREATED", "TRIP_STARTED", "TRIP_COMPLETED"],
        )
        self.assertTrue(all(event["metadata"]["derived_from_booking"] for event in timeline))

    def test_legacy_completed_status_does_not_invent_a_completion_time(self):
        self.booking_a.status = BookingStatus.COMPLETED
        self.booking_a.trip_completed_at = None
        self.booking_a.completed_at = None
        self.db.flush()

        timeline = get_booking_timeline(self.db, self.booking_a.id)

        self.assertEqual([event["event_type"] for event in timeline], ["BOOKING_CREATED"])

    def test_another_agents_trip_is_not_readable(self):
        with self.assertRaises(HTTPException) as ctx:
            self.activity(self.agent_a, self.booking_b.booking_id)

        self.assertEqual(ctx.exception.status_code, 404)

    def test_another_agents_trip_is_indistinguishable_from_a_missing_one(self):
        """Otherwise an agent could probe for other agencies' booking ids."""
        with self.assertRaises(HTTPException) as denied:
            self.activity(self.agent_a, self.booking_b.booking_id)
        with self.assertRaises(HTTPException) as missing:
            self.activity(self.agent_a, "CAB-DOES-NOT-EXIST")

        self.assertEqual(denied.exception.status_code, missing.exception.status_code)
        self.assertEqual(denied.exception.detail, missing.exception.detail)

    def test_non_agents_are_refused(self):
        crew = SimpleNamespace(id=self.agent_a.id, role="crew", agent_profile=None)

        with self.assertRaises(HTTPException) as ctx:
            self.activity(crew, self.booking_a.booking_id)

        self.assertEqual(ctx.exception.status_code, 403)

    def test_last_reached_and_next_destination_use_driver_events(self):
        link = DriverMagicLink(
            booking_id=self.booking_a.id,
            token=_uniq("token"),
            itinerary_stops=[
                {"id": "pickup", "name": "Port gate", "type": "pickup"},
                {"id": "museum", "name": "Museum", "type": "facility"},
                {"id": "market", "name": "Market", "type": "facility"},
            ],
            otp_verified_at=datetime.utcnow(),
        )
        self.db.add(link)
        self.db.flush()
        now = datetime.utcnow()
        # Insert in reverse chronological order to prove timestamps, not row
        # order or planned-stop order, determine the last reached point.
        self.db.add_all([
            DriverMagicLinkReachEvent(
                magic_link_id=link.id, stop_id="pickup", stop_name="Port gate",
                latitude=17.7, longitude=83.3, reached_at=now,
            ),
            DriverMagicLinkReachEvent(
                magic_link_id=link.id, stop_id="museum", stop_name="Museum",
                latitude=17.71, longitude=83.31, reached_at=now - timedelta(minutes=10),
            ),
        ])
        self.db.flush()

        result = self.activity(self.agent_a, self.booking_a.booking_id)

        self.assertEqual(result.last_reached_point.id, "pickup")
        self.assertEqual(result.next_destination.id, "market")
        self.assertEqual(result.stops_reached, 2)

    def test_repeated_stop_arrival_is_idempotent(self):
        link = DriverMagicLink(
            booking_id=self.booking_a.id,
            token=_uniq("token"),
            itinerary_stops=[
                {"id": "pickup", "name": "Port gate", "type": "pickup"},
                {"id": "museum", "name": "Museum", "type": "facility"},
            ],
            otp_verified_at=datetime.utcnow(),
        )
        self.db.add(link)
        self.db.flush()

        first, first_created = mark_stop_reached(
            self.db, link, "museum", 17.71, 83.31
        )
        second, second_created = mark_stop_reached(
            self.db, link, "museum", 99.0, 99.0
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            self.db.query(DriverMagicLinkReachEvent).filter(
                DriverMagicLinkReachEvent.magic_link_id == link.id,
                DriverMagicLinkReachEvent.stop_id == "museum",
            ).count(),
            1,
        )

    def test_completed_and_cancelled_links_reject_new_activity(self):
        link = DriverMagicLink(
            booking_id=self.booking_a.id,
            token=_uniq("token"),
            itinerary_stops=[
                {"id": "pickup", "name": "Port gate", "type": "pickup"},
            ],
            otp_verified_at=datetime.utcnow(),
        )
        self.db.add(link)
        self.db.flush()

        for terminal in (BookingStatus.COMPLETED, BookingStatus.CANCELLED):
            with self.subTest(status=terminal.value):
                self.booking_a.status = terminal
                self.db.flush()
                with self.assertRaises(HTTPException) as error:
                    _require_magic_link_active(link)
                self.assertEqual(error.exception.status_code, 409)

    def test_lifecycle_and_reach_events_form_one_chronological_timeline(self):
        link = DriverMagicLink(
            booking_id=self.booking_a.id,
            token=_uniq("token"),
            itinerary_stops=[
                {"id": "pickup", "name": "Port gate", "type": "pickup"},
                {"id": "museum", "name": "Museum", "type": "facility"},
                {"id": "market", "name": "Market", "type": "facility"},
            ],
            otp_verified_at=datetime.utcnow(),
        )
        self.db.add(link)
        self.db.flush()
        base = datetime.utcnow() - timedelta(hours=1)
        lifecycle = [
            (TimelineEventType.BOOKING_CREATED, 0),
            (TimelineEventType.PROVIDER_NOTIFIED, 2),
            (TimelineEventType.PROVIDER_ACCEPTED, 4),
            (TimelineEventType.DRIVER_ASSIGNED, 6),
            (TimelineEventType.TRIP_STARTED, 8),
            (TimelineEventType.TRIP_COMPLETED, 14),
        ]
        self.db.add_all([
            BookingTimeline(
                booking_id=self.booking_a.id,
                event_type=event_type.value,
                event_time=base + timedelta(minutes=minute),
            )
            for event_type, minute in reversed(lifecycle)
        ])
        self.db.add_all([
            DriverMagicLinkReachEvent(
                magic_link_id=link.id,
                stop_id="museum",
                stop_name="Museum",
                latitude=17.71,
                longitude=83.31,
                reached_at=base + timedelta(minutes=12),
            ),
            DriverMagicLinkReachEvent(
                magic_link_id=link.id,
                stop_id="pickup",
                stop_name="Port gate",
                latitude=17.70,
                longitude=83.30,
                reached_at=base + timedelta(minutes=10),
            ),
        ])
        self.db.flush()

        timeline = get_booking_timeline(self.db, self.booking_a.id)
        labels = [item["event_label"] for item in timeline]
        timestamps = [item["event_time"] for item in timeline]

        self.assertEqual(labels.count("Port gate reached"), 1)
        self.assertEqual(labels.count("Museum reached"), 1)
        self.assertNotIn("Market reached", labels)
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertLess(labels.index("Trip Started"), labels.index("Port gate reached"))
        self.assertLess(labels.index("Museum reached"), labels.index("Trip Completed"))


if __name__ == "__main__":
    unittest.main()
