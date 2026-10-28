"""What every dashboard number means — Document 2 section 22.

The section is titled "no invented welfare or movement data", and Document 3's
Gate G6 names "invented movement/welfare metrics" as a no-go. So this is a
dictionary of definitions before it is any code that counts.

Section 22 requires ten things documented per metric: numerator, denominator,
unique key, event timestamp, eligible status, missing-data treatment,
suppression threshold, aggregation grain, timezone and source version. Each is
a field below, and a metric missing any of them is incomplete rather than
merely undocumented.

**Section 22 also says "Metric sign-off is a pre-build gate."** Nothing here
computes anything. These are the definitions to be agreed before a screen is
built on them, because a screen built on an unagreed metric is a number nobody
can defend and the gate exists to prevent exactly that.

Two kinds of entry need a decision before use:

* `availability=UNAVAILABLE` -- no trustworthy source exists. Section 22 says
  "show unavailable until proven", so the section must say so rather than
  render a zero. A zero and "we cannot tell" look identical on a chart and
  mean opposite things.
* `suppression_threshold` -- the cohort size below which a figure is withheld.
  Document 2 raises it directly: on a vessel with three crew, an incident rate
  names the individual. The default here is a proposal, not a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class Availability:
    READY = "ready"                 # source exists and is trustworthy
    LABELLED = "labelled"           # usable, but the caveat must be shown
    UNAVAILABLE = "unavailable"     # no source; show "not available", never 0


# OWNER DECISION, 26 October 2026: five. A cohort smaller than this has its
# figure withheld rather than shown. Document 2 raises the case it protects --
# on a vessel with three crew, an "incident rate" names the individual -- and
# TEN-19 tests it.
DEFAULT_SUPPRESSION_THRESHOLD = 5

# Owner answer to Q7, 26 October: everything displayed is IST. Stored as UTC
# instants; this is the presentation zone, and aggregation by day uses it.
DISPLAY_TIMEZONE = "Asia/Kolkata"

# Bumped whenever a definition below changes meaning, so a stored report can
# say which definition produced it. Section 22 requires a source version.
DICTIONARY_VERSION = "2026.10.27.1"


@dataclass(frozen=True)
class Metric:
    key: str
    section: str
    label: str
    numerator: str
    denominator: Optional[str]
    unique_key: str
    event_time: str            # the event-dictionary family it is counted by
    eligible_status: str
    missing_data: str
    availability: str
    caveat: str
    aggregation_grain: str = "per vessel, per day"
    suppression_threshold: int = DEFAULT_SUPPRESSION_THRESHOLD
    timezone: str = DISPLAY_TIMEZONE
    source_version: str = DICTIONARY_VERSION

    @property
    def needs_decision(self) -> bool:
        return self.availability == Availability.UNAVAILABLE


METRICS: tuple[Metric, ...] = (
    Metric(
        key="verified_vessels",
        section="overview",
        label="Verified vessels",
        numerator="distinct canonical vessel_id across this company's approved, non-revoked claims",
        denominator=None,
        unique_key="vessel_id",
        event_time="n/a — a state, not an event",
        eligible_status="VesselRoleClaim.status = APPROVED and revoked_at is null",
        missing_data="a claim with no canonical vessel is excluded and counted as unresolved",
        availability="ready",
        caveat="Distinct by vessel, not by claim. A company holding Owner and ISM on one ship is one vessel, not two -- section 22 names this double count directly.",
        suppression_threshold=0,
    ),
    Metric(
        key="qualifying_calls",
        section="vessels_and_port_calls",
        label="Qualifying port calls",
        numerator="vessel_calls whose window intersects an approved role window",
        denominator=None,
        unique_key="vessel_call_id",
        event_time="port_call",
        eligible_status="the call intersects [effective_start, effective_end)",
        missing_data="a call with no started_at is excluded and reported as unresolved",
        availability="labelled",
        caveat="A call may be only partly inside tenure. Listing it does not entitle the company to the events inside it -- section 7 is explicit that each event is attributed by its own timestamp.",
        suppression_threshold=0,
    ),
    Metric(
        key="eligible_crew",
        section="crew_shore_leave",
        label="Crew marked eligible",
        numerator="crew_assignments with shore_pass_eligible true on a qualifying call",
        denominator="crew_assignments on that call",
        unique_key="crew_assignment_id",
        event_time="port_call",
        eligible_status="CrewAssignment.shore_pass_eligible is true",
        missing_data="eligibility is a current flag, not a historical snapshot. A figure for a past call reflects the flag now, not then.",
        availability="labelled",
        caveat="Section 22 asks 'eligibility at what time?' and there is no answer in the schema: the flag carries no history. Label the figure as current state, never as what was true during the call.",
        suppression_threshold=0,
    ),
    Metric(
        key="passes_approved",
        section="crew_shore_leave",
        label="Agent-confirmed shore-leave eligible (operational approval proxy)",
        numerator="call assignments marked eligible by the agent",
        denominator="crew marked eligible on that call",
        unique_key="crew_assignment_id",
        event_time="port_call",
        eligible_status="shore_pass_eligible is true",
        missing_data="Eligibility is current state; there is no historical change snapshot",
        availability="labelled",
        caveat="Agent eligibility is the owner's operational approval proxy, not a physical immigration-issued pass. Historical figures reflect current state.",
        suppression_threshold=0,
    ),
    Metric(
        key="actually_ashore",
        section="crew_shore_leave",
        label="Crew who went ashore",
        numerator="shore_passes with out_time recorded on a qualifying call",
        denominator="passes approved on that call",
        unique_key="shore_pass_id",
        event_time="shore_pass_exit",
        eligible_status="out_time is not null",
        missing_data="a missing in_time is not a crew member who stayed ashore; it is a return nobody wrote down. Report returns separately as unknown.",
        availability="labelled",
        caveat="Written by a person at the gate, so this is a recording of an exit rather than a measurement of one. No manifest inference.",
        suppression_threshold=0,
    ),
    Metric(
        key="booking_activity",
        section="crew_movements",
        label="Cab bookings placed",
        numerator="cab_bookings created inside tenure for a qualifying call",
        denominator=None,
        unique_key="booking_id",
        event_time="cab_booking_placed",
        eligible_status="the booking resolves to a canonical vessel and call",
        missing_data="a booking with no resolved call is excluded, not guessed",
        availability="ready",
        caveat="A booking is not a trip, not a crew member ashore, and not a welfare outcome. Section 22 lists all three.",
        suppression_threshold=0,
    ),
    Metric(
        key="trips_started",
        section="crew_movements",
        label="Trips started",
        numerator="cab_bookings with trip_started_at inside tenure",
        denominator="bookings placed",
        unique_key="booking_id",
        event_time="trip_started",
        eligible_status="trip_started_at is not null",
        missing_data="null means not started, or started and not recorded; counted as not started and labelled",
        availability="ready",
        caveat="A different event time from the booking, so it can fall in a different tenure window. Never attribute trip facts to the booking's timestamp.",
        suppression_threshold=0,
    ),
    Metric(
        key="trips_completed",
        section="crew_movements",
        label="Trips completed",
        numerator="cab_bookings with trip_completed_at inside tenure",
        denominator="trips started",
        unique_key="booking_id",
        event_time="trip_completed",
        eligible_status="trip_completed_at is not null",
        missing_data="as above",
        availability="ready",
        caveat="A completed trip is not proof that anyone re-boarded.",
        suppression_threshold=0,
    ),
    Metric(
        key="sign_on_off",
        section="crew_movements",
        label="Confirmed sign-on and sign-off events",
        numerator="explicit audited crew movement events",
        denominator=None,
        unique_key="assignment_id + movement type",
        event_time="crew_sign_on_off",
        eligible_status="explicit agent confirmation timestamp exists",
        missing_data="Unconfirmed or planned transport is excluded",
        availability="labelled",
        caveat="Agent-confirmed movement events only. Booked or completed transport never confirms boarding.",
        suppression_threshold=0,
    ),
    Metric(
        key="sos_raised",
        section="safety_and_incident_oversight",
        label="SOS raised",
        numerator="crew_sos_requests created inside tenure for a qualifying call",
        denominator=None,
        unique_key="sos_id",
        event_time="sos_raised",
        eligible_status="the request resolves to a canonical vessel and call",
        missing_data="an unresolved request is excluded and reported as unresolved",
        availability="ready",
        caveat="Aggregate only. No precise location and no contact detail: section 10 says this section carries no raw incident or SOS.",
        suppression_threshold=5,
    ),
    Metric(
        key="incidents_reported",
        section="safety_and_incident_oversight",
        label="Incidents",
        numerator="incidents whose occurrence falls inside tenure",
        denominator=None,
        unique_key="incident_id",
        event_time="incident_occurred, falling back to incident_reported",
        eligible_status="the incident resolves to a canonical vessel and call",
        missing_data="a row with no stated occurrence is attributed by its report time and must be labelled; see company_event_dictionary.incident_occurrence",
        availability="labelled",
        caveat="`incidents.occurred_at` was added on 26 Oct 2026 and is null on every earlier row. Those are attributed by report time, which across a handover can be the wrong company -- label, do not hide.",
        suppression_threshold=5,
    ),
    Metric(
        key="incidents_resolved",
        section="safety_and_incident_oversight",
        label="Incidents resolved",
        numerator="incidents with resolved_at inside tenure",
        denominator="incidents reported",
        unique_key="incident_id",
        event_time="incidents.resolved_at",
        eligible_status="resolved_at is not null",
        missing_data="null means unresolved or not recorded as resolved",
        availability="labelled",
        caveat="Resolution is its own event with its own time. An incident raised under one company and resolved under the next belongs, for this metric, to whoever held the vessel when it was resolved -- so the two counts need not reconcile, and a ratio of them is misleading across a handover.",
        suppression_threshold=5,
    ),
    Metric(
        key="welfare_engagement",
        section="welfare_intelligence",
        label="Welfare engagement",
        numerator="completed services in agreed welfare categories",
        denominator=None,
        unique_key="booking_id",
        event_time="trip_completed",
        eligible_status="the service category is on the agreed welfare list",
        missing_data="an uncategorised service is excluded, never assumed welfare",
        availability="unavailable",
        caveat="The categories are not agreed. Section 22 forbids mental-health scores and any claimed wellbeing outcome inferred from rides: a crew member taking a cab to a restaurant is a cab ride, not evidence of welfare. Needs an agreed category list first.",
        suppression_threshold=5,
    ),
    Metric(
        key="sign_ons",
        section="crew_movements",
        label="Confirmed sign-ons",
        numerator="assignments explicitly confirmed signed on",
        denominator=None,
        unique_key="crew_assignment_id",
        event_time="crew_signed_on",
        eligible_status="signed_on_at is recorded",
        missing_data="No confirmation is excluded",
        availability="labelled",
        caveat="Agent-confirmed boarding, not transport completion; counted at the confirmation timestamp.",
        suppression_threshold=0,
    ),
    Metric(
        key="sign_offs",
        section="crew_movements",
        label="Confirmed sign-offs",
        numerator="assignments explicitly confirmed signed off",
        denominator=None,
        unique_key="crew_assignment_id",
        event_time="crew_signed_off",
        eligible_status="signed_off_at is recorded",
        missing_data="No confirmation is excluded",
        availability="labelled",
        caveat="Agent-confirmed departure, not transport completion; counted at the confirmation timestamp.",
        suppression_threshold=0,
    ),
    Metric(
        key="booked_seats",
        section="crew_movements",
        label="Booked passenger seats",
        numerator="sum of booking num_passengers",
        denominator=None,
        unique_key="booking_id",
        event_time="cab_booking_placed",
        eligible_status="resolved booking inside tenure",
        missing_data="Unknown origin excluded",
        availability="ready",
        caveat="Selected seats, not unique crew. Each repeat booking contributes again.",
        suppression_threshold=0,
    ),
    Metric(
        key="returned_seats",
        section="crew_movements",
        label="Returned-to-vessel passenger seats (operational proxy)",
        numerator="selected seats on completed trips",
        denominator=None,
        unique_key="booking_id",
        event_time="trip_completed",
        eligible_status="completed trip inside tenure",
        missing_data="No recorded completion excluded",
        availability="labelled",
        caveat="Completed-trip proxy requested by the owner; not independently verified physical boarding.",
        suppression_threshold=0,
    ),
    Metric(
        key="estimated_ashore_seats",
        section="crew_shore_leave",
        label="Current ashore passenger seats (operational estimate)",
        numerator="selected booking seats with pickup reached and no port-end reached",
        denominator=None,
        unique_key="booking_id",
        event_time="pickup_reached",
        eligible_status="pickup reached within a continuous approved interval covering the snapshot",
        missing_data="Missing or conflicting stop tracking is reported separately",
        availability="labelled",
        caveat="Seat estimate, not unique crew or physical boarding. Counts pickup until port-end reach. Completion is a separate return proxy; conflicting tracking is reported alongside.",
        suppression_threshold=0,
    ),
    Metric(
        key="ashore_uncertain_seats",
        section="crew_shore_leave",
        label="Seats with incomplete or conflicting tracking",
        numerator="seats on started/completed journeys without usable stop evidence, plus completion/return-stop disagreements",
        denominator=None,
        unique_key="booking_id",
        event_time="pickup_reached",
        eligible_status="authorized booking or pickup inside the current continuous interval",
        missing_data="An unrecorded pickup is unknown, never inferred from driver arrival",
        availability="labelled",
        caveat="Coverage indicator; may overlap the estimate when a completed trip has pickup evidence but no port-end reach. No missing tracking is presented as a confirmed exit or return.",
        suppression_threshold=0,
    ),
    Metric(
        key="pending_transfers",
        section="crew_movements",
        label="Planned transfers (not confirmed movement)",
        numerator="open agency transfer requests",
        denominator=None,
        unique_key="agency_transfer_id",
        event_time="transfer_planned",
        eligible_status="not completed, cancelled or declined; planned travel inside tenure",
        missing_data="Requests with conflicting call agency are excluded",
        availability=Availability.LABELLED,
        suppression_threshold=0,
        caveat="Planned transport only; neither a request nor completed transport confirms vessel sign-on/off. Attribution uses scheduled travel time.",
    ),
)

_BY_KEY = {metric.key: metric for metric in METRICS}


class UnknownMetric(KeyError):
    pass


def metric(key: str) -> Metric:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise UnknownMetric(
            f"{key!r} is not in the metric dictionary. Section 22 makes metric "
            "sign-off a pre-build gate, so add and agree the definition before "
            "anything computes it."
        ) from None


def metrics_for(section: str) -> tuple[Metric, ...]:
    return tuple(m for m in METRICS if m.section == section)


def unavailable_metrics() -> tuple[str, ...]:
    """Metrics with no trustworthy source. These render "not available"."""
    return tuple(m.key for m in METRICS if m.availability == Availability.UNAVAILABLE)


def labelled_metrics() -> tuple[str, ...]:
    """Usable, but the caveat has to be shown beside the number."""
    return tuple(m.key for m in METRICS if m.availability == Availability.LABELLED)


# --- the eight sections ------------------------------------------------------
#
# Owner answer to Q6: the same eight sections for all three company roles.
# This says what each contains, which is what a sign-off needs to look at --
# section 22 makes metric sign-off a pre-build gate, so the manifest comes
# before any endpoint that computes a number.

SECTIONS: tuple[str, ...] = (
    "overview",
    "vessels_and_port_calls",
    "crew_shore_leave",
    "crew_movements",
    "safety_and_incident_oversight",
    "welfare_intelligence",
    "reports_and_analytics",
    "settings_and_access",
)

# Sections that carry no metrics of their own. Reports is a rendering of the
# others and Settings carries membership and access, so neither needs an entry
# in the dictionary -- but both must appear in the manifest, or a reader would
# think they had been forgotten.
SECTIONS_WITHOUT_METRICS = frozenset({"reports_and_analytics", "settings_and_access"})


@dataclass(frozen=True)
class SectionManifest:
    key: str
    metrics: tuple[Metric, ...]

    @property
    def buildable(self) -> bool:
        """Whether anything in this section can be computed today."""
        return any(m.availability != Availability.UNAVAILABLE for m in self.metrics)

    @property
    def unavailable(self) -> tuple[str, ...]:
        return tuple(m.key for m in self.metrics if m.needs_decision)


def section_manifest() -> tuple[SectionManifest, ...]:
    """Every section and what it would show. The thing to sign off."""
    return tuple(
        SectionManifest(key=name, metrics=metrics_for(name)) for name in SECTIONS
    )


def sections_with_nothing_to_show() -> tuple[str, ...]:
    """Sections where every metric is unavailable.

    These must say so rather than render an empty chart. Document 3's Gate G6
    names invented movement and welfare metrics as a no-go, and an empty chart
    is the most natural way to invent one by accident.
    """
    return tuple(
        manifest.key
        for manifest in section_manifest()
        if manifest.metrics and not manifest.buildable
    )
