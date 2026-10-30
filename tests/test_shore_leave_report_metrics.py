"""Average shore leave duration, and which day a trip belongs to.

Two defects motivated these tests.

The average summed cab minutes only, then divided by a headcount that also
included crew who walked out on a shore pass without booking a cab. Those people
sat in the denominator and contributed nothing to the numerator, so any day
mixing the two read low.

Separately, trips were selected by `created_at` while shore passes were selected
by `out_time`. The two disagreed across midnight: a cab booked at 23:50 and
driven after midnight was reported on the day it was booked.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

from datetime import datetime
import unittest
import uuid
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_agents import shore_leave_report
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from tests.context_fixtures import immutable_call

REPORT_DATE = "2026-03-05"

# The reporting window is a calendar day *at the port*, and a vessel with no
# configured port falls back to Asia/Kolkata. Times here are therefore written
# on the port's clock: expressing them in UTC makes the midnight cases silently
# vacuous, because 23:50 UTC on the 4th is already 05:20 on the 5th at the port.
PORT_TZ = ZoneInfo("Asia/Kolkata")


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _at(hour, minute=0, day=5):
    """An instant on the reporting day, on the port's clock."""
    return datetime(2026, 3, day, hour, minute, tzinfo=PORT_TZ)


class ShoreLeaveAverageTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.agent_user = User(
            email=_uniq("agent") + "@example.com", hashed_password="x", role="agent"
        )
        self.db.add(self.agent_user)
        self.db.flush()
        self.agent_profile = AgentProfile(
            user_id=self.agent_user.id,
            agency_name="Test Agency",
            location="Test Port",
        )
        self.db.add(self.agent_profile)
        self.db.flush()

        # The report covers the whole port call, so the call needs dates that
        # bracket the days these tests put activity on. `active_vessel_call`
        # copies them from the vessel. Leaving them null made the window
        # collapse onto "now", which is months away from the fixture's data.
        self.vessel = Vessel(
            agent_id=self.agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
            eta=datetime(2026, 3, 1, tzinfo=PORT_TZ),
            etd=datetime(2026, 3, 10, tzinfo=PORT_TZ),
        )
        self.db.add(self.vessel)
        self.db.flush()
        # A report fixture represents a known historical call even when it
        # has no crew/activity; a GET must never manufacture a departed call.
        immutable_call(self.db, self.vessel, self.agent_profile.id)

        self.agent = SimpleNamespace(
            id=self.agent_user.id, role="agent",
            agent_profile=self.agent_profile,
        )

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def crew(self, shore_pass_eligible=True):
        """One crew member on this vessel's manifest."""
        crew_user = User(
            email=_uniq("crew") + "@example.com", hashed_password="x", role="crew"
        )
        self.db.add(crew_user)
        self.db.flush()
        hpid = _uniq("HP")
        # Passport and nationality on both sides, because that pair is what
        # links a manifest row to a profile (HPID spec §4.1). The HPID used to
        # be the key and is no longer one.
        passport = _uniq("P")
        profile = CrewProfile(
            user_id=crew_user.id, full_name="Crew", rank="able_seaman",
            nationality="IN", hpid=hpid, passport_number=passport,
        )
        self.db.add(profile)
        self.db.flush()
        self.db.add(VesselCrew(
            vessel_id=self.vessel.id, name="Crew", rank="able_seaman",
            hp_id=hpid, shore_pass_eligible=shore_pass_eligible,
            passport_number=passport, nationality="IN",
        ))
        self.db.flush()
        immutable_call(self.db, self.vessel, self.agent_profile.id)
        return profile

    def manifest_only_crew(self, shore_pass_eligible=True):
        """Someone on the crew list who has never registered an account."""
        hpid = _uniq("HP")
        self.db.add(VesselCrew(
            vessel_id=self.vessel.id, name="Unregistered Crew", rank="oiler",
            hp_id=hpid, shore_pass_eligible=shore_pass_eligible,
        ))
        self.db.flush()
        immutable_call(self.db, self.vessel, self.agent_profile.id)
        return hpid

    def pass_for(self, crew, out, back):
        self.db.add(
            ShorePass(
                vessel_call_id=immutable_call(
                    self.db, self.vessel, self.agent_profile.id
                ).id,
                crew_profile_id=crew.id,
                shore_pass_id=_uniq("SP"),
                out_time=out,
                in_time=back,
            )
        )
        self.db.flush()

    def trip_for(self, crew, started, completed, created=None, passengers=None,
                 seats=None, status=BookingStatus.COMPLETED):
        # seats left as None keeps the model default of 1, which is one booker
        # and nobody unaccounted for.
        extra = {} if seats is None else {"num_passengers": seats}
        booking = CabBooking(
            booking_id=_uniq("CAB"),
            crew_id=crew.id,
            pickup_address="Gate",
            pickup_lat=0,
            pickup_lng=0,
            drop_address="City",
            drop_lat=0,
            drop_lng=0,
            vehicle_type="ac",
            vehicle_name="Sedan",
            estimated_price=100,
            distance_km=5,
            status=status,
            agency_id=self.agent_profile.id,
            vessel_id=self.vessel.id,
            vessel_call_id=immutable_call(
                self.db, self.vessel, self.agent_profile.id
            ).id,
            context_resolution="CONFIRMED_AGENCY",
            created_at=created or started or _at(10),
            trip_started_at=started,
            trip_completed_at=completed,
            crew_member_ids=passengers,
            **extra,
        )
        self.db.add(booking)
        self.db.flush()
        return booking

    def reach(self, booking, *stop_types):
        """Mark itinerary stops reached, as the driver magic link does."""
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

    def report(self):
        return shore_leave_report(
            vessel_id=self.vessel.id, db=self.db, current_user=self.agent,
        )

    def test_pass_only_crew_are_in_the_average_not_just_the_denominator(self):
        """The defect: cab minutes over a headcount that included pass-only crew.

        One hour in a cab and four hours on a pass average to two and a half
        hours. Dividing the cab hour alone by both people gave 30 minutes.
        """
        rider = self.crew()
        walker = self.crew()
        self.trip_for(rider, started=_at(10), completed=_at(11))
        self.pass_for(walker, out=_at(10), back=_at(14))

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 2)
        self.assertEqual(result.average_duration_minutes, 150)

    def test_a_cab_taken_during_a_pass_is_not_counted_twice(self):
        """A cab ride happens during shore leave; it is not extra time ashore."""
        crew = self.crew()
        self.pass_for(crew, out=_at(9), back=_at(17))
        self.trip_for(crew, started=_at(11), completed=_at(12))

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)
        self.assertEqual(result.average_duration_minutes, 480)

    def test_the_worked_example(self):
        """Two cabs of four, out two hours and three.

            trip 1: 4 crew x 2h =  8 crew-hours
            trip 2: 4 crew x 3h = 12 crew-hours
            total  = 20 crew-hours over 8 crew ashore = 2.5h each

        Crew-hours count each passenger's own time, so a shared cab is not one
        person's two hours but four people's.
        """
        first = [self.crew() for _ in range(4)]
        second = [self.crew() for _ in range(4)]
        self.trip_for(first[0], started=_at(9), completed=_at(11),
                      passengers=[c.hpid for c in first[1:]])
        self.trip_for(second[0], started=_at(13), completed=_at(16),
                      passengers=[c.hpid for c in second[1:]])

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 8)
        self.assertEqual(result.average_duration_minutes, 150)

    def test_a_crew_member_on_two_trips_counts_their_whole_time_once(self):
        """The same person out twice is one head, with both stretches summed.

        Their four hours ashore are four hours, not two trips of two averaged
        back down — and the gap they spent aboard between the two is not leave.
        """
        crew = self.crew()
        self.trip_for(crew, started=_at(9), completed=_at(11))
        self.trip_for(crew, started=_at(14), completed=_at(16))

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)
        self.assertEqual(result.average_duration_minutes, 240)

    def test_crew_still_ashore_are_left_out_of_the_average(self):
        """The figure is leave per *eligible* head, not per completed trip.

        Someone still ashore has no finished duration to contribute yet, so
        they are left out of both the crew-hours and the head count rather than
        counted as a zero. They are reported separately as `still_ashore`.
        """
        returned = self.crew()
        still_out = self.crew()
        self.pass_for(returned, out=_at(10), back=_at(12))
        self.pass_for(still_out, out=_at(10), back=None)

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 2)
        self.assertEqual(result.still_ashore, 1)
        self.assertEqual(result.average_duration_minutes, 120)

    def test_the_average_does_not_sag_when_most_of_the_ship_stays_aboard(self):
        """One ten-minute ride among six eligible crew is a ten-minute ride.

        Eligibility belongs to utilisation — one of six went, so 17% — not to
        the average, which answers how long a crew member who went ashore was
        actually out.
        """
        rider = self.crew()
        for _ in range(5):
            self.crew()
        self.trip_for(rider, started=_at(10), completed=_at(10, 10))

        result = self.report()

        self.assertEqual(result.eligible_for_shore_leave, 6)
        self.assertEqual(result.crew_went_ashore, 1)
        self.assertEqual(result.shore_leave_utilisation_pct, 17)
        self.assertEqual(result.average_duration_minutes, 10)

    def test_an_unregistered_passenger_still_counts_as_ashore(self):
        """A cab booked for three puts three ashore.

        Passengers used to be resolved through crew *accounts*, so anyone on the
        manifest who had never registered was dropped and a three-person booking
        was reported as one.
        """
        booker = self.crew()
        registered = self.crew()
        unregistered_hpid = self.manifest_only_crew()

        self.trip_for(
            booker, started=_at(10), completed=_at(12),
            passengers=[registered.hpid, unregistered_hpid],
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 3)
        self.assertEqual(result.returned_safely, 3)

    def test_a_passenger_id_that_is_not_on_this_manifest_is_ignored(self):
        """Passenger IDs are typed by hand and validated nowhere."""
        booker = self.crew()

        self.trip_for(
            booker, started=_at(10), completed=_at(12),
            passengers=["HP-SOMEONE-ELSES-SHIP", "", None],
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)

    def test_time_back_aboard_between_two_trips_is_not_shore_leave(self):
        """The "12h 21m" defect: gaps between separate trips billed as leave.

        Each person's time ashore was the envelope from first departure to last
        return, so a crew member who took a short cab in the morning and another
        in the evening was reported as having spent the whole day ashore —
        including the hours they were back aboard in between.
        """
        crew = self.crew()
        self.trip_for(crew, started=_at(8), completed=_at(9))
        self.trip_for(crew, started=_at(19), completed=_at(20, 21))

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)
        # Two hours and 21 minutes ashore, not the 12h 21m envelope.
        self.assertEqual(result.average_duration_minutes, 141)

    def test_unresolved_legacy_completion_is_excluded_until_review(self):
        """Legacy rows recorded the status but not the times.

        A completion alone does not prove whose historical call this belongs to.
        It cannot follow the passenger's present-day crew manifest into a report.
        """
        crew = self.crew()
        self.db.add(CabBooking(
            booking_id=_uniq("CAB"), crew_id=crew.id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type="ac", vehicle_name="Sedan",
            estimated_price=100, distance_km=5,
            status=BookingStatus.COMPLETED,
            created_at=_at(9),
        ))
        self.db.flush()

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 0)
        self.assertEqual(result.returned_safely, 0)
        self.assertEqual(result.still_ashore, 0)
        self.assertTrue(result.all_returned)
        # Back aboard, but with no timestamps there is nothing to measure.
        self.assertIsNone(result.average_duration_minutes)

    def test_a_cab_ashore_and_back_counts_as_returned_without_a_shore_pass(self):
        """Returns used to be read off shore-pass in_time alone.

        Crew who went ashore by cab had no pass to sign back in, so the report
        showed "0 / 1 returned" and "1 still ashore" beside their own completed
        trip.
        """
        crew = self.crew()
        self.trip_for(crew, started=_at(10), completed=_at(12))

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)
        self.assertEqual(result.returned_safely, 1)
        self.assertEqual(result.still_ashore, 0)
        self.assertTrue(result.all_returned)

    def test_anyone_who_went_ashore_counts_towards_the_average(self):
        """Eligibility gates utilisation, not time actually spent ashore.

        A crew member the manifest did not mark eligible who nonetheless went
        ashore was really ashore, and their hours are real. Excluding them would
        report an average over people while quietly dropping some of them.
        """
        eligible = self.crew()
        ineligible = self.crew(shore_pass_eligible=False)
        self.pass_for(eligible, out=_at(10), back=_at(12))
        self.pass_for(ineligible, out=_at(9), back=_at(17))

        result = self.report()

        self.assertEqual(result.eligible_for_shore_leave, 1)
        self.assertEqual(result.crew_went_ashore, 2)
        # (120 + 480) / 2
        self.assertEqual(result.average_duration_minutes, 300)


class GroupBookingPassengerTests(ShoreLeaveAverageTests):
    """A shared cab puts everyone in it ashore, not just whoever booked it.

    crew_member_ids holds HeyPorts IDs typed in by the booking crew member. They
    are validated nowhere, so the report resolves them through this vessel's
    manifest and ignores anything that does not land on it.
    """

    def test_fellow_passengers_on_the_manifest_are_counted(self):
        booker = self.crew()
        passenger = self.crew()
        self.trip_for(
            booker, started=_at(10), completed=_at(12),
            passengers=[passenger.hpid],
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 2)
        # Both were in the same cab, so both were ashore for the same two hours.
        self.assertEqual(result.average_duration_minutes, 120)

    def test_an_unrecognised_id_does_not_invent_a_person(self):
        booker = self.crew()
        self.trip_for(
            booker, started=_at(10), completed=_at(12),
            passengers=["HP-typo-nobody", ""],
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)

    def test_another_vessels_crew_cannot_be_added_to_this_report(self):
        booker = self.crew()

        other_user = User(
            email=_uniq("other") + "@example.com", hashed_password="x", role="crew"
        )
        self.db.add(other_user)
        self.db.flush()
        stranger = CrewProfile(
            user_id=other_user.id, full_name="Stranger", rank="able_seaman",
            nationality="IN", hpid=_uniq("HP"),
        )
        self.db.add(stranger)
        self.db.flush()

        self.trip_for(
            booker, started=_at(10), completed=_at(12),
            passengers=[stranger.hpid],
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 1)


class ReportingWindowTests(ShoreLeaveAverageTests):
    """Which trips fall inside the port call the report covers.

    The report spans the whole call — arrival to departure, or to now while the
    ship is still alongside — because that is when an agent sends it. What still
    has to hold is that the window ends: activity after the vessel sailed
    belongs to no part of this call.
    """

    def test_a_trip_is_measured_from_when_it_ran_not_when_it_was_booked(self):
        """Booked at 23:30 the night before, driven at 00:30 this morning."""
        crew = self.crew()
        self.trip_for(
            crew,
            created=_at(23, 30, day=4),
            started=_at(0, 30), completed=_at(1, 30),
        )

        result = self.report()

        self.assertEqual(result.completed_trips, 1)
        self.assertEqual(result.crew_went_ashore, 1)
        self.assertEqual(result.average_duration_minutes, 60)

    def test_a_later_day_of_the_same_call_is_included(self):
        """The report covers the call, not one date.

        This trip ran the day after the one that used to be "the report date",
        and it belongs to the same port call, so it counts. Reporting a single
        day is what made a call print zeros on every date but one.
        """
        crew = self.crew()
        self.trip_for(
            crew,
            created=_at(23, 30),
            started=_at(0, 30, day=6), completed=_at(1, 30, day=6),
        )

        result = self.report()

        self.assertEqual(result.completed_trips, 1)
        self.assertEqual(result.crew_went_ashore, 1)

    def test_a_trip_after_the_vessel_sailed_is_not_on_the_report(self):
        """The window still ends — at the departure, not at midnight.

        The fixture's call runs to 10 March, so a trip two days later belongs to
        no part of it and must not be swept in by a report that now spans days.
        """
        crew = self.crew()
        self.trip_for(crew, started=_at(10, day=12), completed=_at(12, day=12))

        result = self.report()

        self.assertEqual(result.completed_trips, 0)
        self.assertEqual(result.crew_went_ashore, 0)

    def test_a_booking_that_never_started_puts_nobody_ashore(self):
        """Booked but never driven: nobody has gone ashore on it yet.

        This used to count as a departure, which was wrong twice over. The crew
        member is sitting aboard waiting for a driver, and because the trip
        never runs there is nothing for them to finish — so they were also
        counted as never having returned, and stayed "still ashore" forever.
        """
        crew = self.crew()
        self.db.add(CabBooking(
            booking_id=_uniq("CAB"), crew_id=crew.id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type="ac", vehicle_name="Sedan",
            estimated_price=100, distance_km=5,
            status=BookingStatus.PENDING,
            created_at=_at(9),
        ))
        self.db.flush()

        result = self.report()

        self.assertEqual(result.completed_trips, 0)
        self.assertEqual(result.crew_went_ashore, 0)
        self.assertEqual(result.still_ashore, 0)
        self.assertIsNone(result.average_duration_minutes)


class ResolvedSafetyRecordTests(ShoreLeaveAverageTests):
    """`Outstanding issues` counts SOS alerts and incidents as one ledger.

    The resolved figure beside it counted incidents only, so a closed SOS was
    held against the day as raised and never shown as answered.
    """

    def sos(self, status):
        from app.db.models.crew_sos import CrewSos

        self.db.add(CrewSos(
            agency_id=self.agent_profile.id, vessel_id=self.vessel.id,
            status=status, created_at=_at(11),
        ))
        self.db.flush()

    def incident(self, status):
        from app.db.models.incident import Incident, IncidentStatus, IncidentType

        self.db.add(Incident(
            type=IncidentType.CREW, title="Test", description="Test",
            status=status, agency_id=self.agent_profile.id,
            vessel_id=self.vessel.id, created_at=_at(11),
        ))
        self.db.flush()
        return IncidentStatus

    def test_a_closed_sos_counts_as_resolved(self):
        from app.db.models.incident import IncidentStatus

        self.sos("CLOSED")
        self.incident(IncidentStatus.RESOLVED)

        result = self.report()

        self.assertEqual(result.sos_raised, 1)
        self.assertEqual(result.incidents_reported, 1)
        self.assertEqual(result.safety_resolved, 2)
        self.assertEqual(result.outstanding_issues, 0)

    def test_an_open_sos_is_not_resolved_and_stays_outstanding(self):
        from app.db.models.incident import IncidentStatus

        self.sos("ACTIVE")
        self.incident(IncidentStatus.RESOLVED)

        result = self.report()

        self.assertEqual(result.safety_resolved, 1)
        self.assertEqual(result.outstanding_issues, 1)

    def test_a_cancelled_sos_counts_as_resolved(self):
        self.sos("CANCELLED")

        result = self.report()

        self.assertEqual(result.safety_resolved, 1)
        self.assertEqual(result.outstanding_issues, 0)

    def test_the_incident_only_figure_is_left_untouched(self):
        """Snapshots frozen before this fall back to it, so it must not move."""
        from app.db.models.incident import IncidentStatus

        self.sos("CLOSED")
        self.incident(IncidentStatus.RESOLVED)

        result = self.report()

        self.assertEqual(result.incidents_resolved, 1)


class SeatsTakenTests(ShoreLeaveAverageTests):
    """Crew a booking paid for but named nowhere still went ashore.

    Most of a manifest has no HeyPorts account, and `crew_member_ids` is only
    filled in when the booking crew typed their shipmates' IDs by hand. Counting
    only the people the system could name reported two crew ashore against two
    four-seat cabs, which is the KR & Sons defect: eight seats, two names.
    """

    def test_a_four_seat_cab_puts_four_ashore(self):
        booker = self.crew()

        self.trip_for(booker, started=_at(10), completed=_at(12), seats=4)

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 4)

    def test_the_unnamed_seats_come_back_when_the_trip_completes(self):
        booker = self.crew()

        self.trip_for(booker, started=_at(10), completed=_at(12), seats=4)

        result = self.report()

        self.assertEqual(result.returned_safely, 4)
        self.assertEqual(result.still_ashore, 0)
        self.assertTrue(result.all_returned)

    def test_the_kr_and_sons_shape(self):
        """Two completed four-seat cabs on a 24-crew ship: eight ashore."""
        for _ in range(2):
            self.trip_for(self.crew(), started=_at(10), completed=_at(13), seats=4)
        for _ in range(22):
            self.manifest_only_crew()

        result = self.report()

        self.assertEqual(result.crew_onboard, 24)
        self.assertEqual(result.crew_went_ashore, 8)
        self.assertEqual(result.returned_safely, 8)
        self.assertEqual(result.completed_trips, 2)
        self.assertEqual(result.shore_leave_utilisation_pct, 33)

    def test_seats_carry_the_trips_own_time_into_the_average(self):
        """Four people in a cab for two hours is four two-hour departures.

        Not one — the average answers "how long was a crew member ashore", so
        every seat has to bring its own time or the figure means nothing.
        """
        booker = self.crew()

        self.trip_for(booker, started=_at(10), completed=_at(12), seats=4)

        result = self.report()

        self.assertEqual(result.average_duration_minutes, 120)

    def test_seats_are_still_ashore_once_the_pickup_is_reached(self):
        booker = self.crew()

        booking = self.trip_for(
            booker, started=_at(10), completed=None, seats=3,
            status=BookingStatus.ON_TRIP,
        )
        self.reach(booking, "pickup")

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 3)
        self.assertEqual(result.still_ashore, 3)
        self.assertFalse(result.all_returned)

    def test_a_running_trip_with_no_reach_event_is_neither_ashore_nor_returned(self):
        """Owner decision: no reach evidence means no claim in either direction.

        It must not quietly fall into returns by subtraction either, which is
        what made the report and the dashboard tile disagree before.
        """
        booker = self.crew()

        self.trip_for(
            booker, started=_at(10), completed=None, seats=3,
            status=BookingStatus.ON_TRIP,
        )

        result = self.report()

        self.assertEqual(result.still_ashore, 0)
        self.assertEqual(result.returned_safely, 0)

    def test_reaching_the_port_again_returns_the_seats(self):
        booker = self.crew()

        booking = self.trip_for(
            booker, started=_at(10), completed=None, seats=3,
            status=BookingStatus.ON_TRIP,
        )
        self.reach(booking, "pickup", "trip_end")

        result = self.report()

        self.assertEqual(result.returned_safely, 3)
        self.assertEqual(result.still_ashore, 0)
        self.assertTrue(result.all_returned)

    def test_a_cancelled_booking_takes_nobody_ashore_however_many_seats(self):
        booker = self.crew()

        self.trip_for(
            booker, started=_at(10), completed=_at(12), seats=4,
            status=BookingStatus.CANCELLED,
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 0)

    def test_naming_more_crew_than_seats_does_not_subtract_anyone(self):
        """A booker who under-declared the seat count still took three ashore."""
        booker = self.crew()
        one = self.crew()
        two = self.crew()

        self.trip_for(
            booker, started=_at(10), completed=_at(12), seats=1,
            passengers=[one.hpid, two.hpid],
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 3)

    def test_named_passengers_are_not_double_counted_as_seats(self):
        """Four seats, all four named: four people, not eight."""
        booker = self.crew()
        others = [self.crew().hpid for _ in range(3)]

        self.trip_for(
            booker, started=_at(10), completed=_at(12), seats=4,
            passengers=others,
        )

        result = self.report()

        self.assertEqual(result.crew_went_ashore, 4)

    def test_utilisation_never_exceeds_the_whole_crew(self):
        """Seats count journeys, so a crew riding twice can outnumber itself."""
        booker = self.crew()

        self.trip_for(booker, started=_at(8), completed=_at(9), seats=4)
        self.trip_for(booker, started=_at(18), completed=_at(19), seats=4)

        result = self.report()

        self.assertGreater(result.crew_went_ashore, result.eligible_for_shore_leave)
        self.assertEqual(result.shore_leave_utilisation_pct, 100)


if __name__ == "__main__":
    unittest.main()


class TileAndReportAgreeTests(SeatsTakenTests):
    """The reason app/services/crew_ashore.py exists.

    The dashboard tile and this report answered "who is ashore" from different
    evidence and disagreed in front of the agent. Both now read the same
    per-stop reach events, so these assert the two figures directly against
    each other rather than each against a constant.
    """

    def _tile(self, *crew):
        from app.services.crew_ashore import crew_ashore_count
        return crew_ashore_count(self.db, [c.id for c in crew])

    def test_they_agree_while_the_crew_are_ashore(self):
        booker = self.crew()
        booking = self.trip_for(
            booker, started=_at(10), completed=None, seats=3,
            status=BookingStatus.ON_TRIP,
        )
        self.reach(booking, "pickup")

        self.assertEqual(self.report().still_ashore, self._tile(booker))

    def test_they_agree_once_the_port_stop_is_reached(self):
        booker = self.crew()
        booking = self.trip_for(
            booker, started=_at(10), completed=None, seats=3,
            status=BookingStatus.ON_TRIP,
        )
        self.reach(booking, "pickup", "trip_end")

        self.assertEqual(self.report().still_ashore, self._tile(booker))
        self.assertEqual(self._tile(booker), 0)

    def test_they_agree_when_the_evidence_is_missing(self):
        """Both must decline to claim, rather than one guessing."""
        booker = self.crew()
        self.trip_for(
            booker, started=_at(10), completed=None, seats=3,
            status=BookingStatus.ON_TRIP,
        )

        self.assertEqual(self.report().still_ashore, self._tile(booker))
        self.assertEqual(self._tile(booker), 0)
