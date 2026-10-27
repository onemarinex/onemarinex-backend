"""Who is off the ship right now.

The dashboard tile and the shore leave report were answering this from
different evidence, so they disagreed in front of the agent. One calculation,
used by both.

**Owner decision, 26 October 2026.** A cab trip's own status is not evidence
that anyone left the ship. Three signals existed and disagreed: the driver API
calls a trip ``arrived`` when the driver reaches the *pickup*, the magic-link
itinerary records a separate reach event per stop, and this module used to count
any trip with a start timestamp. The agent-facing number now comes from the
per-stop reach events only:

* seats join the count when the stop of type ``pickup`` is marked reached;
* they leave it when the stop of type ``trip_end`` (the return to port) is
  marked reached, or when the booking completes.

A trip that has started but carries no usable reach event is **not** counted and
**not** treated as returned. It is reported separately as unknown, so a thin
evidence period reads as low confidence rather than as an empty port. The
driver's generic ``arrived`` state is never substituted for a passenger boarding,
and a return to port is never inferred from an arbitrary intermediate stop.

Shore passes keep contributing on their own evidence: ``out_time`` set with no
``in_time`` is a recorded physical exit, which is stronger than a cab pickup,
and the owner decision is silent on them.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.driver_magic_link import DriverMagicLink, DriverMagicLinkReachEvent
from app.db.models.shore_pass import ShorePass

# Only these stop types carry the meaning we need. A "drop" or "facility" stop
# says the cab arrived somewhere in town, not that anyone went ashore or came
# back, so neither opens nor closes the count.
_DEPARTURE_STOP_TYPES = {"pickup"}
_RETURN_STOP_TYPES = {"trip_end"}

_ENDED_TRIP_STATUSES = {BookingStatus.COMPLETED, BookingStatus.CANCELLED}

# Booking states this module distinguishes.
ASHORE = "ASHORE"
RETURNED = "RETURNED"
NOT_STARTED = "NOT_STARTED"
UNKNOWN = "UNKNOWN"


@dataclass
class AshoreBreakdown:
    """Counted seats plus the evidence quality behind them."""

    named: Set[int] = field(default_factory=set)
    seats: int = 0
    unknown_bookings: int = 0
    unknown_seats: int = 0

    @property
    def count(self) -> int:
        return len(self.named) + self.seats

    @property
    def has_full_coverage(self) -> bool:
        return self.unknown_bookings == 0


def _stop_types_by_id(stops: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    """Map each itinerary stop id to its semantic type.

    Reach events record only ``stop_id``; the type lives in the itinerary, so
    the two have to be resolved against each other. Compound stops are split
    into ``<id>__1``, ``<id>__2`` and inherit the parent's type, so a prefix
    match covers them.
    """
    types: Dict[str, str] = {}
    for stop in stops or []:
        if not isinstance(stop, dict):
            continue
        stop_id = str(stop.get("id") or "").strip()
        stop_type = str(stop.get("type") or "").strip().lower()
        if stop_id and stop_type:
            types[stop_id] = stop_type
    return types


def _reached_types(
    stop_types: Dict[str, str], reached_ids: Iterable[str]
) -> Set[str]:
    found: Set[str] = set()
    for reached in reached_ids:
        reached = str(reached or "").strip()
        if not reached:
            continue
        stop_type = stop_types.get(reached)
        if stop_type is None and "__" in reached:
            # A split compound stop keeps its parent's type.
            stop_type = stop_types.get(reached.split("__", 1)[0])
        if stop_type:
            found.add(stop_type)
    return found


def _trip_has_begun(booking: CabBooking) -> bool:
    return bool(booking.trip_started_at or booking.started_at)


def booking_ashore_state(
    booking: CabBooking, reached_ids: Iterable[str] = ()
) -> str:
    """Classify one booking from its reach events alone."""
    if booking.status == BookingStatus.CANCELLED:
        return NOT_STARTED
    if booking.status == BookingStatus.COMPLETED:
        # Owner decision: a completed trip is the return-to-vessel proxy.
        return RETURNED

    link = booking.magic_link
    stop_types = _stop_types_by_id(link.itinerary_stops if link else None)
    reached = _reached_types(stop_types, reached_ids)

    if reached & _RETURN_STOP_TYPES:
        return RETURNED
    if reached & _DEPARTURE_STOP_TYPES:
        return ASHORE
    # No usable reach evidence. A started trip is unknown, not ashore: counting
    # it would be the driver-state substitution the owner ruled out.
    return UNKNOWN if _trip_has_begun(booking) else NOT_STARTED


def _reached_ids_by_booking(
    db: Session, bookings: List[CabBooking]
) -> Dict[int, List[str]]:
    """One query for every booking's reach events, rather than one each."""
    if not bookings:
        return {}
    rows = (
        db.query(DriverMagicLink.booking_id, DriverMagicLinkReachEvent.stop_id)
        .join(
            DriverMagicLinkReachEvent,
            DriverMagicLinkReachEvent.magic_link_id == DriverMagicLink.id,
        )
        .filter(DriverMagicLink.booking_id.in_([b.id for b in bookings]))
        .all()
    )
    reached: Dict[int, List[str]] = {}
    for booking_id, stop_id in rows:
        reached.setdefault(booking_id, []).append(stop_id)
    return reached


def crew_ashore_breakdown(
    db: Session,
    crew_profile_ids: Iterable[int],
    *,
    extra_people=None,
) -> AshoreBreakdown:
    """Seats ashore now, with the unknown remainder reported separately.

    `extra_people` resolves a booking to everyone it carried, so group
    passengers count individually rather than only the crew member who booked.

    Named crew are counted by identity, so someone with both an open pass and a
    cab ride ashore is one person. Only the remainder of a trip's seats is added
    on top.
    """
    breakdown = AshoreBreakdown()
    ids = [i for i in crew_profile_ids if i]
    if not ids:
        return breakdown

    breakdown.named = {
        row[0]
        for row in db.query(ShorePass.crew_profile_id)
        .filter(
            ShorePass.crew_profile_id.in_(ids),
            ShorePass.out_time.isnot(None),
            ShorePass.in_time.is_(None),
        )
        .all()
        if row[0]
    }

    open_bookings = (
        db.query(CabBooking)
        .filter(
            CabBooking.crew_id.in_(ids),
            CabBooking.status.notin_(list(_ENDED_TRIP_STATUSES)),
        )
        .all()
    )
    reached_by_booking = _reached_ids_by_booking(db, open_bookings)

    for booking in open_bookings:
        state = booking_ashore_state(booking, reached_by_booking.get(booking.id, ()))
        if state == UNKNOWN:
            breakdown.unknown_bookings += 1
            breakdown.unknown_seats += booking.num_passengers or 0
            continue
        if state != ASHORE:
            continue
        on_this_trip = (
            {person for person in extra_people(booking) if person}
            if extra_people is not None
            else ({booking.crew_id} if booking.crew_id else set())
        )
        breakdown.named.update(on_this_trip)
        breakdown.seats += max(0, (booking.num_passengers or 0) - len(on_this_trip))

    return breakdown


def crew_ashore_ids(
    db: Session,
    crew_profile_ids: Iterable[int],
    *,
    extra_people=None,
) -> Set[int]:
    """Distinct crew evidenced as ashore — an open pass, or a cab past pickup."""
    return crew_ashore_breakdown(
        db, crew_profile_ids, extra_people=extra_people
    ).named


def crew_ashore_count(
    db: Session,
    crew_profile_ids: Iterable[int],
    *,
    extra_people=None,
) -> int:
    """How many crew are ashore, counting the seats a cab could not name.

    Excludes bookings with no usable reach evidence; read
    `crew_ashore_breakdown` when the caller needs to show that coverage.
    """
    return crew_ashore_breakdown(
        db, crew_profile_ids, extra_people=extra_people
    ).count
