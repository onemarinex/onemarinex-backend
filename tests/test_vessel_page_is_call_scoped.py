"""A vessel's page shows the call it is on, not the ship's whole history.

MV JIM MING 82 was onboarded a second time, which correctly opened a new port
call — and its page then showed the trips, incidents and SOS alerts of calls
131 and 133 appended to the new one. Crew, trips and safety records all belong
to a call; a ship that comes back starts empty.

Rows that predate `vessel_call_id` carry no call at all. Those are still shown,
but only where they fall inside the current call's window, so an unattributable
record from an earlier visit does not resurface under today's arrival.

Runs against the configured database inside a transaction that is rolled back.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.api.v1.routes_incidents import agent_incident_list
from app.api.v1.routes_trips import get_trip_monitoring
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_sos import CrewSos
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.session import engine


NOW = datetime.now(timezone.utc)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class VesselPageCallScopingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        user = User(email=_uniq("agent") + "@example.com",
                    hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        self.profile = AgentProfile(
            user_id=user.id, agency_name=_uniq("Agency"), location="Port",
            assigned_port="port_test",
        )
        self.db.add(self.profile)
        self.db.flush()
        self.agent = SimpleNamespace(id=user.id, role="agent",
                                     agent_profile=self.profile)

        self.vessel = Vessel(
            agent_id=user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
            agency_name=self.profile.agency_name,
            eta=NOW - timedelta(days=1), etd=NOW + timedelta(days=2),
        )
        self.db.add(self.vessel)
        self.db.flush()

        # The visit before last, and the one the ship is on now.
        self.previous = self._call(
            started=NOW - timedelta(days=30), ended=NOW - timedelta(days=28))
        self.current = self._call(started=NOW - timedelta(days=1), ended=None)

        crew_user = User(email=_uniq("crew") + "@example.com",
                         hashed_password="x", role="crew")
        self.db.add(crew_user)
        self.db.flush()
        self.crew = CrewProfile(
            user_id=crew_user.id, full_name="Test Crew", rank="Third Officer",
            nationality="IN", hpid=_uniq("HP"),
        )
        self.db.add(self.crew)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def _call(self, *, started, ended):
        call = VesselCall(
            vessel_id=self.vessel.id, agency_id=self.profile.id,
            vessel_name=self.vessel.name, imo_number=self.vessel.imo_number,
            port_name="port_test", status="DEPARTED" if ended else "ACTIVE",
            eta=started, etd=ended or NOW + timedelta(days=2),
            started_at=started, ended_at=ended,
        )
        self.db.add(call)
        self.db.flush()
        return call

    def _trip(self, *, call, created):
        booking = CabBooking(
            booking_id=_uniq("CAB"), crew_id=self.crew.id,
            agency_id=self.profile.id, vessel_id=self.vessel.id,
            vessel_call_id=call.id if call else None,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type="ac", vehicle_name="Sedan",
            estimated_price=100, distance_km=5,
            status=BookingStatus.COMPLETED,
            created_at=created,
        )
        self.db.add(booking)
        self.db.flush()
        return booking

    def _incident(self, *, call, created, title):
        incident = Incident(
            incident_id=_uniq("INC"), type=IncidentType.CREW, title=title,
            description="test", status=IncidentStatus.ACTIVE,
            vessel_id=self.vessel.id, agency_id=self.profile.id,
            vessel_call_id=call.id if call else None,
            created_at=created,
        )
        self.db.add(incident)
        self.db.flush()
        return incident

    def _sos(self, *, call, created):
        sos = CrewSos(
            crew_profile_id=self.crew.id, agency_id=self.profile.id,
            vessel_id=self.vessel.id,
            vessel_call_id=call.id if call else None,
            vessel=self.vessel.name, port_name="port_test",
            status="ACTIVE", created_at=created,
        )
        self.db.add(sos)
        self.db.flush()
        return sos

    def trips(self):
        result = get_trip_monitoring(
            vessel_id=self.vessel.id, db=self.db, current_user=self.agent)
        return [*result.ongoing, *result.requested, *result.completed]

    def safety(self):
        return agent_incident_list(
            vessel_id=self.vessel.id, include_sos=True,
            db=self.db, current_user=self.agent,
        )["incidents"]

    def test_previous_calls_records_do_not_follow_the_ship_back(self):
        self._trip(call=self.previous, created=NOW - timedelta(days=29))
        self._incident(call=self.previous, created=NOW - timedelta(days=29),
                       title="Last visit")
        self._sos(call=self.previous, created=NOW - timedelta(days=29))

        self.assertEqual(self.trips(), [])
        self.assertEqual(self.safety(), [])

    def test_this_calls_records_are_shown(self):
        self._trip(call=self.current, created=NOW - timedelta(hours=6))
        self._incident(call=self.current, created=NOW - timedelta(hours=6),
                       title="This visit")
        self._sos(call=self.current, created=NOW - timedelta(hours=6))

        self.assertEqual(len(self.trips()), 1)
        kinds = sorted(record["kind"] for record in self.safety())
        self.assertEqual(kinds, ["incident", "sos"])

    def test_the_two_calls_are_kept_apart(self):
        self._trip(call=self.previous, created=NOW - timedelta(days=29))
        self._trip(call=self.current, created=NOW - timedelta(hours=6))
        self._incident(call=self.previous, created=NOW - timedelta(days=29),
                       title="Last visit")
        self._incident(call=self.current, created=NOW - timedelta(hours=6),
                       title="This visit")

        self.assertEqual(len(self.trips()), 1)
        titles = [record["title"] for record in self.safety()]
        self.assertEqual(titles, ["This visit"])

    def test_records_with_no_call_require_reconciliation(self):
        """Call-less rows remain in owned history but never inherit today's call."""
        self._trip(call=None, created=NOW - timedelta(hours=6))
        self._trip(call=None, created=NOW - timedelta(days=29))
        self._incident(call=None, created=NOW - timedelta(hours=6),
                       title="Unattributed, this visit")
        self._incident(call=None, created=NOW - timedelta(days=29),
                       title="Unattributed, long ago")

        self.assertEqual(len(self.trips()), 0)
        titles = [record["title"] for record in self.safety()]
        self.assertEqual(titles, [])


if __name__ == "__main__":
    unittest.main()
