"""Eligibility reaching the report, and who counts as ashore.

Two reported defects, both from one value living in two places.

Eligibility is stored on the manifest row the vessel screen edits *and* on the
crew assignment operational reports read. Manifest upload and manual crew
creation wrote both; the eligibility toggle and the general crew edit wrote only
the manifest. So an agent could mark eight crew eligible, see eight on the
vessel page, and have the report still show one.

"Crew ashore" was answered from open shore passes alone on the dashboard, while
the shore leave report also treated a started cab trip as evidence. Crew leaving
by cab without a pass showed as zero ashore beside a trip that was underway.

Per the owner decision of 26 October 2026 a started trip is no longer evidence
on its own: the seats join the count when the magic-link PICKUP stop is marked
reached and leave it at the TRIP END (PORT) stop. A started trip with no reach
event is reported as unknown rather than counted, so these tests mark the stops
the driver would have marked.

Runs against the configured database inside a transaction that is always
rolled back.
"""

from datetime import datetime, timedelta, timezone
import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking, VehicleType
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class _Base(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        user = User(email=_uniq("agent") + "@example.com",
                    hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        self.profile = AgentProfile(
            user_id=user.id, agency_name="Test Agency", location="Port",
            assigned_port="port_test",
        )
        self.db.add(self.profile)
        self.db.flush()
        self.agent_user = user
        self.agent = SimpleNamespace(id=user.id, role="agent",
                                     agent_profile=self.profile)

        self.vessel = Vessel(
            agent_id=user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        self.db.flush()

        self.call = VesselCall(
            vessel_id=self.vessel.id, agency_id=self.profile.id,
            vessel_name=self.vessel.name, imo_number=self.vessel.imo_number,
            port_name="port_test", status="ACTIVE",
        )
        self.db.add(self.call)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def crew(self, *, eligible=False, with_account=True):
        """A manifest row, its assignment, and optionally a crew account."""
        hpid = _uniq("HP")
        profile = None
        if with_account:
            user = User(email=_uniq("crew") + "@example.com",
                        hashed_password="x", role="crew")
            self.db.add(user)
            self.db.flush()
            profile = CrewProfile(
                user_id=user.id, full_name="Crew", rank="able_seaman",
                nationality="IN", hpid=hpid,
            )
            self.db.add(profile)
            self.db.flush()

        row = VesselCrew(
            vessel_id=self.vessel.id, name="Crew", rank="able_seaman",
            hp_id=hpid, shore_pass_eligible=eligible,
        )
        self.db.add(row)
        self.db.flush()

        assignment = CrewAssignment(
            vessel_call_id=self.call.id,
            crew_profile_id=profile.id if profile else None,
            vessel_crew_id=row.id, crew_name="Crew", rank="able_seaman",
            hpid=hpid, shore_pass_eligible=eligible,
        )
        self.db.add(assignment)
        self.db.flush()
        return SimpleNamespace(row=row, assignment=assignment, profile=profile)


class EligibilitySyncTests(_Base):
    def test_the_toggle_reaches_the_assignment_the_report_reads(self):
        """The control an agent actually uses to mark crew eligible."""
        from app.api.v1.routes_vessels import EligibilityUpdateIn, update_crew_eligibility

        member = self.crew(eligible=False)

        update_crew_eligibility(
            vessel_id=self.vessel.id, crew_id=member.row.id,
            body=EligibilityUpdateIn(shore_pass_eligible=True),
            current_user=self.agent, db=self.db,
        )

        self.db.refresh(member.assignment)
        self.assertTrue(member.row.shore_pass_eligible)
        self.assertTrue(member.assignment.shore_pass_eligible)

    def test_the_toggle_also_takes_eligibility_away(self):
        from app.api.v1.routes_vessels import EligibilityUpdateIn, update_crew_eligibility

        member = self.crew(eligible=True)

        update_crew_eligibility(
            vessel_id=self.vessel.id, crew_id=member.row.id,
            body=EligibilityUpdateIn(shore_pass_eligible=False),
            current_user=self.agent, db=self.db,
        )

        self.db.refresh(member.assignment)
        self.assertFalse(member.assignment.shore_pass_eligible)

    def test_the_general_crew_edit_syncs_too(self):
        from app.api.v1.routes_vessels import CrewMemberUpdate, update_crew_member

        member = self.crew(eligible=False)

        update_crew_member(
            vessel_id=self.vessel.id, crew_id=member.row.id,
            body=CrewMemberUpdate(shore_pass_eligible=True),
            current_user=self.agent, db=self.db,
        )

        self.db.refresh(member.assignment)
        self.assertTrue(member.assignment.shore_pass_eligible)

    def test_editing_another_field_leaves_eligibility_alone(self):
        from app.api.v1.routes_vessels import CrewMemberUpdate, update_crew_member

        member = self.crew(eligible=True)

        update_crew_member(
            vessel_id=self.vessel.id, crew_id=member.row.id,
            body=CrewMemberUpdate(name="Renamed"),
            current_user=self.agent, db=self.db,
        )

        self.db.refresh(member.assignment)
        self.assertTrue(member.assignment.shore_pass_eligible)

    def test_eight_eligible_on_the_manifest_are_eight_on_the_assignment(self):
        """The reported case: the screen said 8, the report saw 1."""
        from app.api.v1.routes_vessels import EligibilityUpdateIn, update_crew_eligibility

        members = [self.crew(eligible=False) for _ in range(8)]
        for member in members:
            update_crew_eligibility(
                vessel_id=self.vessel.id, crew_id=member.row.id,
                body=EligibilityUpdateIn(shore_pass_eligible=True),
                current_user=self.agent, db=self.db,
            )

        eligible = self.db.query(CrewAssignment).filter(
            CrewAssignment.vessel_call_id == self.call.id,
            CrewAssignment.shore_pass_eligible.is_(True),
        ).count()

        self.assertEqual(eligible, 8)


class CrewAshoreTests(_Base):
    def _trip(self, crew, *, status, started=True, passengers=None, seats=None):
        now = datetime.now(timezone.utc)
        # seats left as None keeps the model default of 1: one booker, nobody
        # unaccounted for.
        extra = {} if seats is None else {"num_passengers": seats}
        booking = CabBooking(
            booking_id=_uniq("CAB"), crew_id=crew.profile.id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type=VehicleType.AC, vehicle_name="Sedan",
            estimated_price=100, distance_km=5, status=status,
            crew_member_ids=passengers,
            trip_started_at=(now - timedelta(hours=1)) if started else None,
            **extra,
        )
        self.db.add(booking)
        self.db.flush()
        return booking

    def _reach(self, booking, *stop_types):
        """Mark itinerary stops reached, exactly as the driver link does.

        The reach event stores only stop_id, so the booking needs its real
        itinerary for the id to resolve back to a semantic type.
        """
        from app.db.models.driver_magic_link import (
            DriverMagicLink, DriverMagicLinkReachEvent,
        )
        from app.services.magic_link_service import _default_itinerary_stops

        stops = _default_itinerary_stops(booking)
        link = DriverMagicLink(
            booking_id=booking.id, token=_uniq("tok"), itinerary_stops=stops,
        )
        self.db.add(link)
        self.db.flush()
        for stop in stops:
            if stop["type"] in stop_types:
                self.db.add(DriverMagicLinkReachEvent(
                    magic_link_id=link.id, stop_id=stop["id"],
                    stop_name=stop["name"], latitude=stop.get("lat") or 0,
                    longitude=stop.get("lng") or 0,
                ))
        self.db.flush()
        self.db.expire(booking)
        return link

    def _breakdown(self, members):
        from app.services.crew_ashore import crew_ashore_breakdown
        return crew_ashore_breakdown(
            self.db, [m.profile.id for m in members if m.profile])

    def _count(self, members):
        from app.services.crew_ashore import crew_ashore_count
        return crew_ashore_count(
            self.db, [m.profile.id for m in members if m.profile])

    def test_reaching_the_pickup_puts_its_crew_ashore(self):
        """The reported case: one trip underway, the tile read zero."""
        member = self.crew()
        booking = self._trip(member, status=BookingStatus.ON_TRIP)
        self._reach(booking, "pickup")

        self.assertEqual(self._count([member]), 1)

    def test_a_started_trip_with_no_reach_event_is_unknown_not_ashore(self):
        """Owner decision: never substitute the driver state for a boarding."""
        member = self.crew()
        self._trip(member, status=BookingStatus.ON_TRIP, seats=4)

        breakdown = self._breakdown([member])
        self.assertEqual(breakdown.count, 0)
        self.assertEqual(breakdown.unknown_bookings, 1)
        self.assertEqual(breakdown.unknown_seats, 4)
        self.assertFalse(breakdown.has_full_coverage)

    def test_reaching_the_port_again_brings_them_back(self):
        member = self.crew()
        booking = self._trip(member, status=BookingStatus.ON_TRIP, seats=4)
        self._reach(booking, "pickup", "trip_end")

        self.assertEqual(self._count([member]), 0)

    def test_reaching_the_drop_alone_does_not_bring_them_back(self):
        """A stop in town is not the return to port, and is never read as one."""
        member = self.crew()
        booking = self._trip(member, status=BookingStatus.ON_TRIP, seats=4)
        self._reach(booking, "pickup", "drop")

        self.assertEqual(self._count([member]), 4)

    def test_a_driver_merely_assigned_does_not(self):
        """Waiting for a cab is not being ashore."""
        member = self.crew()
        self._trip(member, status=BookingStatus.DRIVER_ASSIGNED, started=False)

        self.assertEqual(self._count([member]), 0)

    def test_a_completed_trip_means_they_came_back(self):
        member = self.crew()
        self._trip(member, status=BookingStatus.COMPLETED)

        self.assertEqual(self._count([member]), 0)

    def test_an_open_shore_pass_still_counts(self):
        member = self.crew()
        self.db.add(ShorePass(
            crew_profile_id=member.profile.id, shore_pass_id=_uniq("SP"),
            out_time=datetime.now(timezone.utc) - timedelta(hours=2),
        ))
        self.db.flush()

        self.assertEqual(self._count([member]), 1)

    def test_one_person_with_both_counts_once(self):
        """Pass rows were counted, not people."""
        member = self.crew()
        for _ in range(2):
            self.db.add(ShorePass(
                crew_profile_id=member.profile.id, shore_pass_id=_uniq("SP"),
                out_time=datetime.now(timezone.utc) - timedelta(hours=2),
            ))
        self.db.flush()
        self._trip(member, status=BookingStatus.ON_TRIP)

        self.assertEqual(self._count([member]), 1)

    def test_group_passengers_count_when_the_caller_resolves_them(self):
        booker = self.crew()
        rider = self.crew()
        booking = self._trip(booker, status=BookingStatus.ON_TRIP,
                             passengers=[rider.assignment.hpid])
        self._reach(booking, "pickup")

        from app.services.crew_ashore import crew_ashore_count

        by_hpid = {rider.assignment.hpid: rider.profile.id}

        def people(trip):
            found = {trip.crew_id} if trip.crew_id else set()
            for hpid in (trip.crew_member_ids or []):
                if hpid in by_hpid:
                    found.add(by_hpid[hpid])
            return found

        count = crew_ashore_count(
            self.db, [booker.profile.id, rider.profile.id], extra_people=people)

        self.assertEqual(count, 2)

    def test_a_cab_puts_every_seat_ashore_not_just_the_booker(self):
        """The tile has to agree with the report, which counts seats.

        Most of a ship's company has no account and a group booking names only
        the person who made it, so counting identities alone showed one crew
        ashore beside a report reading eight for the same vessel and day. An
        agent had no way to tell which of the two was lying.
        """
        member = self.crew()
        booking = self._trip(member, status=BookingStatus.ON_TRIP, seats=4)
        self._reach(booking, "pickup")

        self.assertEqual(self._count([member]), 4)

    def test_named_riders_are_not_counted_twice_as_seats(self):
        """Four seats, all four named: four people, not eight."""
        booker = self.crew()
        riders = [self.crew() for _ in range(3)]
        booking = self._trip(booker, status=BookingStatus.ON_TRIP, seats=4,
                             passengers=[r.assignment.hpid for r in riders])
        self._reach(booking, "pickup")

        from app.services.crew_ashore import crew_ashore_count

        by_hpid = {r.assignment.hpid: r.profile.id for r in riders}

        def people(trip):
            found = {trip.crew_id} if trip.crew_id else set()
            for hpid in (trip.crew_member_ids or []):
                if hpid in by_hpid:
                    found.add(by_hpid[hpid])
            return found

        count = crew_ashore_count(
            self.db,
            [booker.profile.id] + [r.profile.id for r in riders],
            extra_people=people,
        )

        self.assertEqual(count, 4)

    def test_seats_on_a_trip_that_never_started_put_nobody_ashore(self):
        member = self.crew()
        self._trip(member, status=BookingStatus.DRIVER_ASSIGNED,
                   started=False, seats=4)

        self.assertEqual(self._count([member]), 0)

    def test_seats_come_back_when_the_trip_completes(self):
        member = self.crew()
        self._trip(member, status=BookingStatus.COMPLETED, seats=4)

        self.assertEqual(self._count([member]), 0)


if __name__ == "__main__":
    unittest.main()
