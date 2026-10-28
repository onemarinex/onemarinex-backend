"""Which timestamp is authoritative for each event family, and how good it is.

Document 2 section 12 gives candidate timestamps per event family and then
says the dictionary "must be frozen in an event dictionary **after source
schema inspection**", and that "any missing/ambiguous source timestamp results
in explicit data quality status; do not silently assume".

This is that dictionary, written after inspecting the schema on 26 October
2026. The inspection changed several of the candidates, because the schema does
not hold what the document hoped it did.

**The finding that shapes everything here: no table in this database records
when an event occurred. Only when it was recorded.** A search for any column
named like "occurred" returns nothing. So for several families the honest
canonical timestamp is a reporting time, and the dashboard must say so rather
than present it as the moment the thing happened.

That distinction is not pedantry at a handover. An incident reported on 2 July
about something that happened on 29 June is attributed to the incoming company
if the dictionary pretends `created_at` is the occurrence. Document 2's
instruction -- attribute each event by its own occurrence timestamp -- cannot be
fully satisfied by this schema, and saying so is the point of `quality`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class Quality:
    """How well the stored timestamp answers "when did this happen?"."""

    # The stored time is the event. A booking is placed when the row is made.
    AUTHORITATIVE = "authoritative"
    # The stored time is when someone told us, which may be later than the
    # event. Usable, but never presentable as the moment of occurrence.
    RECORDED_NOT_OCCURRED = "recorded_not_occurred"
    # Nothing in the schema answers the question. The policy must deny rather
    # than substitute a timestamp that happens to be nearby.
    PLANNED = "planned"  # scheduled transport, never a physical movement
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class EventTime:
    family: str
    table: Optional[str]
    column: Optional[str]
    quality: str
    caveat: str

    @property
    def usable(self) -> bool:
        return self.quality != Quality.UNAVAILABLE


EVENT_DICTIONARY: dict[str, EventTime] = {
    "cab_booking_placed": EventTime(
        family="cab_booking_placed",
        table="cab_bookings",
        column="created_at",
        quality=Quality.AUTHORITATIVE,
        caveat=(
            "The booking was placed when the row was made, so this is the event. "
            "It does not mean the trip ran, completed, or that anyone went ashore."
        ),
    ),
    "trip_started": EventTime(
        family="trip_started",
        table="cab_bookings",
        column="trip_started_at",
        quality=Quality.AUTHORITATIVE,
        caveat=(
            "`cab_bookings.started_at` also exists and is a different column. It "
            "is unpopulated -- zero rows carry it, measured -- so trip_started_at "
            "is the live one. Do not attribute trip facts to booking creation."
        ),
    ),
    "trip_completed": EventTime(
        family="trip_completed",
        table="cab_bookings",
        column="trip_completed_at",
        quality=Quality.AUTHORITATIVE,
        caveat=(
            "`cab_bookings.completed_at` also exists and is unpopulated, same as "
            "above. A completed trip is not proof of re-boarding."
        ),
    ),
    "shore_pass_approved": EventTime(
        family="shore_pass_approved",
        table="shore_passes",
        column=None,
        quality=Quality.UNAVAILABLE,
        caveat=(
            "The document allows this 'if actually recorded'. It is not: "
            "shore_passes has no approved_at column. `created_at` is when the "
            "pass row was made, which is not the approval, and substituting it "
            "is exactly the silent assumption section 12 forbids."
        ),
    ),
    "shore_pass_exit": EventTime(
        family="shore_pass_exit",
        table="shore_passes",
        column="out_time",
        quality=Quality.RECORDED_NOT_OCCURRED,
        caveat=(
            "Recorded by a person at the gate, so it is a recording of an exit "
            "rather than a measurement of one. Only trustworthy where the pass "
            "is correctly call-attributed."
        ),
    ),
    "shore_pass_return": EventTime(
        family="shore_pass_return",
        table="shore_passes",
        column="in_time",
        quality=Quality.RECORDED_NOT_OCCURRED,
        caveat=(
            "Same as the exit, and thinner: of 8 passes in the development "
            "database, in_time is set on none. Absence is not a return that "
            "did not happen; it is a return nobody wrote down."
        ),
    ),
    "incident_occurred": EventTime(
        family="incident_occurred",
        table="incidents",
        column="occurred_at",
        quality=Quality.AUTHORITATIVE,
        caveat=(
            "When the incident happened, as stated by whoever reported it. "
            "Null on every row written before the column existed, and on any "
            "report where the person did not say -- use `incident_occurrence` "
            "rather than this entry directly, because the quality of an "
            "incident's timestamp is a property of the row, not the family."
        ),
    ),
    "incident_reported": EventTime(
        family="incident_reported",
        table="incidents",
        column="created_at",
        quality=Quality.RECORDED_NOT_OCCURRED,
        caveat=(
            "When we were told, which is the fallback for a row with no stated "
            "occurrence. The gap between the two is however long it took "
            "someone to open the app, and at a handover that gap decides which "
            "company the incident belongs to -- so a row resolved to this "
            "entry is attributable only approximately, and must be labelled."
        ),
    ),
    "sos_raised": EventTime(
        family="sos_raised",
        table="crew_sos_requests",
        column="created_at",
        quality=Quality.AUTHORITATIVE,
        caveat=(
            "An SOS is raised by the crew member in the moment, so creation is "
            "the event. Never substitute acknowledged_at or closed_at, which "
            "measure the response rather than the emergency."
        ),
    ),
    "crew_signed_on": EventTime(
        family="crew_signed_on",
        table="crew_assignments",
        column="signed_on_at",
        quality=Quality.AUTHORITATIVE,
        caveat="Explicit agent confirmation; not inferred from transport.",
    ),
    "crew_signed_off": EventTime(
        family="crew_signed_off",
        table="crew_assignments",
        column="signed_off_at",
        quality=Quality.AUTHORITATIVE,
        caveat="Explicit agent confirmation; not inferred from transport.",
    ),
    "crew_sign_on_off": EventTime(
        family="crew_sign_on_off",
        table="crew_assignments",
        column=None,
        quality=Quality.AUTHORITATIVE,
        caveat="Combined metric counts sign-on and sign-off timestamps independently.",
    ),
    "pickup_reached": EventTime(
        family="pickup_reached",
        table="driver_magic_link_reach_events",
        column="reached_at",
        quality=Quality.AUTHORITATIVE,
        caveat="Explicit pickup/port-end stop reach; a recording, not independently verified physical boarding.",
    ),
    "transfer_planned": EventTime(
        family="transfer_planned",
        table="agency_transfers",
        column="travel_at",
        quality=Quality.PLANNED,
        caveat="Scheduled travel time on a request; never an actual sign-on/off event.",
    ),
    "port_call": EventTime(
        family="port_call",
        table="vessel_calls",
        column="started_at",
        quality=Quality.AUTHORITATIVE,
        caveat=(
            "The call's own range, for display. Section 7 is explicit that it "
            "must not authorize the events inside it: a call spanning a "
            "handover belongs partly to each company, by each event's own time."
        ),
    ),
}


class UnknownEventFamily(KeyError):
    pass


def event_time(family: str) -> EventTime:
    try:
        return EVENT_DICTIONARY[family]
    except KeyError:
        raise UnknownEventFamily(
            f"{family!r} has no entry in the event dictionary. Add one after "
            "inspecting the source schema; do not fall back to created_at."
        ) from None


def families_with_no_usable_time() -> tuple[str, ...]:
    """Families the dashboard cannot attribute, and must say so about."""
    return tuple(
        name for name, entry in sorted(EVENT_DICTIONARY.items()) if not entry.usable
    )


def families_reporting_rather_than_occurrence() -> tuple[str, ...]:
    """Families whose time is when we were told, not when it happened.

    These are the ones that misattribute across a handover, so a dashboard
    showing them near a tenure boundary owes the reader a caveat.
    """
    return tuple(
        name
        for name, entry in sorted(EVENT_DICTIONARY.items())
        if entry.quality == Quality.RECORDED_NOT_OCCURRED
    )


def incident_occurrence(incident) -> tuple[Optional[datetime], EventTime]:
    """The timestamp to attribute an incident by, and how good it is.

    Incidents are the one family whose quality varies per row rather than per
    family: `occurred_at` was added on 26 October 2026 and is null on every row
    written before that, and on any report where the person did not say when it
    happened.

    A stated occurrence is authoritative. Without one this falls back to the
    report time and says so, rather than returning a timestamp that looks the
    same as a real occurrence -- which is the whole point of the distinction.
    """
    stated = getattr(incident, "occurred_at", None)
    if stated is not None:
        return stated, EVENT_DICTIONARY["incident_occurred"]
    return getattr(incident, "created_at", None), EVENT_DICTIONARY["incident_reported"]
