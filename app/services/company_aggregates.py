"""Dashboard figures, filtered before they are aggregated.

Document 2 section 7: "For each aggregate, filter source rows BEFORE GROUP BY
and before joining dimension tables; never calculate fleet-wide total then
filter displayed vessel labels."

That is a structural requirement, not a convention, so this module offers one
primitive -- `tenure_scoped` -- and every figure is built on it. A caller
cannot accidentally count first and filter afterwards, because there is no
unfiltered query to start from.

Two things every figure carries, rather than returning a bare number:

* **the metric's definition**, so a screen can render the caveat beside the
  figure rather than in a tooltip nobody opens;
* **whether it was suppressed**, because a withheld figure and a zero are
  different answers and must not look the same.

Ready and labelled metrics use their explicitly declared event sources.
Labelled estimates/proxies always surface their caveats. Unavailable sources
remain "not available" rather than an invented zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.db.models.cab_booking import CabBooking
from app.db.models.shipping_company import VesselRoleClaim
from app.services.company_fleet_identity import claim_matches_vessel, identity_key, operational_ids
from app.services.company_metrics import Availability, Metric, metric


@dataclass(frozen=True)
class Figure:
    """One number, with everything needed to show it honestly."""

    metric: Metric
    value: Optional[int]
    suppressed: bool = False
    available: bool = True

    @property
    def display(self) -> str:
        if not self.available:
            return "Not available"
        if self.suppressed:
            return "Withheld"
        return str(self.value)

    @property
    def caveat(self) -> str:
        return self.metric.caveat


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Period:
    """A half-open reporting window, [start, end).

    Half-open to match the tenure intervals it composes with. A quarterly
    report and the next one must not both contain the instant between them.
    """

    start: datetime
    end: datetime


def tenure_scoped(
    db: Session, query, *, company_id: int, vessel_column, occurred_column,
    period: Optional["Period"] = None, include_declared: bool = False,
):
    """Restrict a query to rows this company held the vessel for, at that row's
    own moment.

    The join is the whole point. Filtering by vessel alone would hand a company
    every event on a ship it held for one month of five years; filtering by a
    report period alone would hand it events from before it took over. The
    condition is per row: this vessel, and this row's own timestamp inside an
    approved claim's half-open interval.

    `EXISTS` rather than a join, so a vessel covered by two of the company's
    roles does not duplicate the row and inflate the count.

    A `period` narrows it further, for a report over a year or a quarter. It
    is applied **as well as** tenure and never instead of it -- TEN-04 asks for
    "a scoped annual report", and a report that filtered by the year alone
    would hand a company the whole of a ship's year on the strength of one
    month's management. Both conditions are per row and both must hold.
    """
    covering = (
        db.query(VesselRoleClaim.id)
        .filter(
            VesselRoleClaim.company_id == company_id,
            VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED") if include_declared else ("APPROVED",)),
            VesselRoleClaim.revoked_at.is_(None),
            claim_matches_vessel(vessel_column),
            VesselRoleClaim.effective_start <= occurred_column,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > occurred_column,
            ),
        )
        .exists()
    )
    query = query.filter(occurred_column.isnot(None), covering)
    if period is not None:
        # Half-open, matching the tenure intervals beside it, so a row on the
        # boundary belongs to exactly one period rather than to both.
        query = query.filter(
            occurred_column >= period.start, occurred_column < period.end)
    return query


def _suppressed(
    definition: Metric, value: int, cohort_size: Optional[int] = None
) -> Figure:
    """Apply the threshold once, in one place.

    Zero is never suppressed: withholding "none happened" tells the reader
    nothing and hides nothing.
    """
    if (
        value > 0
        and (cohort_size if cohort_size is not None else value)
        < definition.suppression_threshold
    ):
        return Figure(metric=definition, value=None, suppressed=True)
    return Figure(metric=definition, value=value)


def _counted(
    db: Session, *, metric_key: str, company_id: int, occurred_column,
    model=CabBooking, vessel_column=None, extra=None,
    period: Optional["Period"] = None,
) -> Figure:
    definition = metric(metric_key)
    if definition.availability == Availability.UNAVAILABLE:
        return Figure(metric=definition, value=None, available=False)

    vessel_column = vessel_column if vessel_column is not None else model.vessel_id
    query = db.query(func.count(func.distinct(model.id)))
    if extra is not None:
        query = query.filter(extra)
    query = tenure_scoped(
        db,
        query,
        company_id=company_id,
        vessel_column=vessel_column,
        occurred_column=occurred_column,
        period=period,
    )
    if hasattr(model, "context_resolution"):
        from app.services.event_origin import resolved_origin

        query = query.filter(resolved_origin(model))
    value = query.scalar() or 0
    cohort = None
    if definition.suppression_threshold and hasattr(model, "crew_profile_id"):
        cohort = (
            query.with_entities(
                func.count(func.distinct(model.crew_profile_id))
            ).scalar()
            or 0
        )
    return _suppressed(definition, value, cohort)


# --- the five ready metrics --------------------------------------------------

def verified_vessels(db: Session, *, company_id: int, at: datetime) -> Figure:
    """Distinct vessels this company held at one instant.

    Distinct by vessel, not by claim: a company holding Owner and ISM on one
    ship is one vessel. Section 22 names that double count directly.
    """
    definition = metric("verified_vessels")
    moment = _aware(at)
    value = (
        db.query(func.count(func.distinct(identity_key())))
        .filter(
            VesselRoleClaim.company_id == company_id,
            VesselRoleClaim.status == "APPROVED",
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.effective_start <= moment,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > moment,
            ),
        )
        .scalar()
    ) or 0
    # A fleet size is not a cohort of people, so it is not suppressed: knowing
    # a company manages three ships identifies nobody.
    return Figure(metric=definition, value=value)


def booking_activity(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    return _counted(
        db, metric_key="booking_activity", company_id=company_id, period=period,
        occurred_column=CabBooking.created_at,
    )


def trips_started(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    return _counted(
        db, metric_key="trips_started", company_id=company_id, period=period,
        occurred_column=CabBooking.trip_started_at,
    )


def trips_completed(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    return _counted(
        db, metric_key="trips_completed", company_id=company_id, period=period,
        occurred_column=CabBooking.trip_completed_at,
    )


def sos_raised(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    from app.db.models.crew_sos import CrewSos

    return _counted(
        db, metric_key="sos_raised", company_id=company_id, period=period,
        model=CrewSos, occurred_column=CrewSos.created_at,
        vessel_column=CrewSos.vessel_id,
    )


# --- the five labelled metrics -----------------------------------------------
#
# Computable, but each carries a caveat the screen has to show beside the
# number rather than hide in a tooltip. `Figure.caveat` is populated for all of
# them, so a renderer that drops it is the bug rather than this module.


def qualifying_calls(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    """Port calls that intersect an approved role window.

    Counted by the call's own start, because a call is a period and this is the
    list of them. Listing a call does not entitle the company to the events
    inside it -- those are attributed one at a time, which is what every other
    figure here does.
    """
    from app.db.models.vessel_call import VesselCall

    covering = db.query(VesselRoleClaim.id).filter(
        VesselRoleClaim.company_id == company_id,
        VesselRoleClaim.status == "APPROVED",
        VesselRoleClaim.revoked_at.is_(None),
        claim_matches_vessel(VesselCall.vessel_id),
        VesselRoleClaim.effective_start
        < func.coalesce(VesselCall.ended_at, func.now()),
        or_(
            VesselRoleClaim.effective_end.is_(None),
            VesselRoleClaim.effective_end > VesselCall.started_at,
        ),
    )
    if period:
        covering = covering.filter(
            VesselRoleClaim.effective_start < period.end,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > period.start,
            ),
        )
    query = db.query(func.count(VesselCall.id)).filter(covering.exists())
    if period:
        query = query.filter(
            VesselCall.started_at < period.end,
            func.coalesce(VesselCall.ended_at, func.now()) > period.start,
        )
    return Figure(metric("qualifying_calls"), query.scalar() or 0)


def eligible_crew(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    """Crew marked eligible for shore leave.

    Counted by the assignment's start, not by the flag's change: the flag has
    no history, so a figure for a past call reflects the flag now. The caveat
    says exactly that, and there is no way to do better without a schema that
    records when eligibility changed.
    """
    from app.db.models.crew_assignment import CrewAssignment
    from app.db.models.vessel_call import VesselCall

    definition = metric("eligible_crew")
    query = (
        db.query(func.count(func.distinct(CrewAssignment.id)))
        .join(VesselCall, VesselCall.id == CrewAssignment.vessel_call_id)
        .filter(CrewAssignment.shore_pass_eligible.is_(True))
    )
    query = tenure_scoped(
        db, query, company_id=company_id,
        vessel_column=VesselCall.vessel_id,
        occurred_column=CrewAssignment.started_at,
        period=period,
    )
    return _suppressed(definition, query.scalar() or 0)


def _shore_pass_figure(db, *, metric_key, company_id, time_column, period=None):
    from app.db.models.shore_pass import ShorePass
    from app.db.models.vessel_call import VesselCall

    definition = metric(metric_key)
    query = (
        db.query(func.count(func.distinct(ShorePass.id)))
        .join(VesselCall, VesselCall.id == ShorePass.vessel_call_id)
    )
    query = tenure_scoped(
        db, query, company_id=company_id,
        vessel_column=VesselCall.vessel_id, occurred_column=time_column,
        period=period,
    )
    return _suppressed(definition, query.scalar() or 0)


def actually_ashore(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    """Crew whose exit was written down at the gate.

    A recording of an exit, not a measurement of one. A missing return is not
    a crew member still ashore; it is a return nobody recorded, which is why
    the return figure is separate rather than subtracted from this.
    """
    from app.db.models.shore_pass import ShorePass

    return _shore_pass_figure(
        db, metric_key="actually_ashore", company_id=company_id,
        time_column=ShorePass.out_time, period=period,
    )


def incidents_reported(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    """Incidents attributed by when they happened, falling back to when they
    were reported.

    COALESCE in that order, matching `company_event_dictionary
    .incident_occurrence`: a stated occurrence is authoritative and a null one
    is the report time. Across a handover the fallback can attribute to the
    wrong company, which the caveat says rather than hides.
    """
    from app.db.models.incident import Incident

    return _counted(
        db,
        metric_key="incidents_reported",
        company_id=company_id,
        occurred_column=func.coalesce(Incident.occurred_at, Incident.created_at),
        model=Incident,
        period=period,
    )


def incidents_resolved(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    """Resolution is its own event, at its own time.

    An incident raised under one company and resolved under the next counts
    here for whoever held the vessel when it was resolved, so this and
    `incidents_reported` need not reconcile -- and a ratio of the two is
    misleading across a handover, which the caveat states.
    """
    from app.db.models.incident import Incident

    return _counted(
        db,
        metric_key="incidents_resolved",
        company_id=company_id,
        occurred_column=Incident.resolved_at,
        model=Incident,
        period=period,
    )


def _movement_count(db, company_id, column, key, period=None):
    from app.db.models.crew_assignment import CrewAssignment
    from app.db.models.vessel_call import VesselCall

    query = db.query(func.count(CrewAssignment.id)).join(
        VesselCall, VesselCall.id == CrewAssignment.vessel_call_id
    )
    query = tenure_scoped(
        db,
        query,
        company_id=company_id,
        vessel_column=VesselCall.vessel_id,
        occurred_column=column,
        period=period,
    )
    return Figure(metric(key), query.scalar() or 0)


def sign_ons(db, *, company_id, period=None):
    from app.db.models.crew_assignment import CrewAssignment

    return _movement_count(
        db, company_id, CrewAssignment.signed_on_at, "sign_ons", period
    )


def sign_offs(db, *, company_id, period=None):
    from app.db.models.crew_assignment import CrewAssignment

    return _movement_count(
        db, company_id, CrewAssignment.signed_off_at, "sign_offs", period
    )


def sign_on_off(db, *, company_id, period=None):
    return Figure(
        metric("sign_on_off"),
        sign_ons(db, company_id=company_id, period=period).value
        + sign_offs(db, company_id=company_id, period=period).value,
    )


def _seats(db, company_id, key, occurred_column, period=None):
    from app.services.event_origin import resolved_origin

    query = db.query(func.coalesce(func.sum(CabBooking.num_passengers), 0)).filter(
        resolved_origin(CabBooking)
    )
    query = tenure_scoped(
        db,
        query,
        company_id=company_id,
        vessel_column=CabBooking.vessel_id,
        occurred_column=occurred_column,
        period=period,
    )
    return Figure(metric(key), query.scalar() or 0)


def booked_seats(db, *, company_id, period=None):
    return _seats(db, company_id, "booked_seats", CabBooking.created_at, period)


def returned_seats(db, *, company_id, period=None):
    from app.db.models.cab_booking import BookingStatus

    # Completion timestamp alone is not enough for the owner's COMPLETED proxy.
    from app.services.event_origin import resolved_origin

    query = db.query(func.coalesce(func.sum(CabBooking.num_passengers), 0)).filter(
        resolved_origin(CabBooking), CabBooking.status == BookingStatus.COMPLETED
    )
    query = tenure_scoped(
        db,
        query,
        company_id=company_id,
        vessel_column=CabBooking.vessel_id,
        occurred_column=CabBooking.trip_completed_at,
        period=period,
    )
    return Figure(metric("returned_seats"), query.scalar() or 0)


READY_FIGURES = {
    "verified_vessels": verified_vessels,
    "booking_activity": booking_activity,
    "trips_started": trips_started,
    "trips_completed": trips_completed,
    "sos_raised": sos_raised,
}

LABELLED_FIGURES = {
    "qualifying_calls": qualifying_calls,
    "eligible_crew": eligible_crew,
    "actually_ashore": actually_ashore,
    "incidents_reported": incidents_reported,
    "incidents_resolved": incidents_resolved,
}

COMPUTABLE_FIGURES = {
    **READY_FIGURES,
    **LABELLED_FIGURES,
    "sign_ons": sign_ons,
    "sign_offs": sign_offs,
    "sign_on_off": sign_on_off,
    "booked_seats": booked_seats,
    "returned_seats": returned_seats,
}


def operational_passes_approved(db, *, company_id, period=None):
    value = eligible_crew(db, company_id=company_id, period=period).value
    return Figure(metric("passes_approved"), value)


COMPUTABLE_FIGURES["passes_approved"] = operational_passes_approved


def _ashore_snapshot(db, company_id, period=None, *, vessel_call_id=None, include_declared=False):
    """Snapshot only within a continuous approved tenure covering the instant.

    A claim gap must never let a historic pickup follow a returning company.
    All facts are time-filtered before counting; no private participant DTOs leave.
    """
    from datetime import timedelta
    from sqlalchemy.orm import selectinload
    from app.db.models.driver_magic_link import (
        DriverMagicLink,
        DriverMagicLinkReachEvent,
    )
    from app.db.models.cab_booking import BookingStatus
    from app.services.event_origin import resolved_origin

    at = (
        min(_aware(period.end) - timedelta(microseconds=1), datetime.now(timezone.utc))
        if period
        else datetime.now(timezone.utc)
    )
    claim_query = (
        db.query(VesselRoleClaim)
        .filter(
            VesselRoleClaim.company_id == company_id,
            VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED") if include_declared else ("APPROVED",)),
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.effective_start <= at,
        )
    )
    if vessel_call_id is not None:
        from app.db.models.vessel_call import VesselCall
        call = db.get(VesselCall, vessel_call_id)
        if call is None:
            return 0, 0
        claim_query = claim_query.filter(claim_matches_vessel(call.vessel_id))
    claims = claim_query.order_by(VesselRoleClaim.vessel_id, VesselRoleClaim.effective_start).all()
    merged = {}
    for c in claims:
        for vessel_id in operational_ids(db, c):
            merged.setdefault(vessel_id, []).append((_aware(c.effective_start), _aware(c.effective_end)))
    # Claims with and without an operational link may now cover the same
    # vessel. Merge after identity resolution, in chronological order.
    for vessel_id, intervals in list(merged.items()):
        rows = []
        for start, end in sorted(intervals, key=lambda interval: interval[0]):
            if rows and (rows[-1][1] is None or rows[-1][1] >= start):
                previous = rows[-1]
                rows[-1] = (previous[0], None if previous[1] is None or end is None else max(previous[1], end))
            else:
                rows.append((start, end))
        merged[vessel_id] = rows
    windows = {
        v: (start, end)
        for v, intervals in merged.items()
        for start, end in intervals
        if start <= at and (end is None or at < end)
    }
    if not windows:
        return 0, 0
    predicates = []
    for vessel_id, (start, _) in windows.items():
        recorded = (
            db.query(DriverMagicLink.id)
            .join(
                DriverMagicLinkReachEvent,
                DriverMagicLink.id == DriverMagicLinkReachEvent.magic_link_id,
            )
            .filter(
                DriverMagicLink.booking_id == CabBooking.id,
                DriverMagicLinkReachEvent.reached_at >= start,
                DriverMagicLinkReachEvent.reached_at <= at,
            )
            .exists()
        )
        predicates.append(
            and_(
                CabBooking.vessel_id == vessel_id,
                or_(
                    and_(CabBooking.created_at >= start, CabBooking.created_at <= at),
                    recorded,
                ),
            )
        )
    query = (
        db.query(CabBooking)
        .filter(
            resolved_origin(CabBooking),
            CabBooking.status != BookingStatus.CANCELLED,
            or_(*predicates),
        )
        .options(
            selectinload(CabBooking.magic_link).selectinload(
                DriverMagicLink.reach_events
            )
        )
    )
    if vessel_call_id is not None:
        query = query.filter(CabBooking.vessel_call_id == vessel_call_id)
    seats = uncertain = 0
    for booking in query.yield_per(500):
        start, _ = windows[booking.vessel_id]
        link = booking.magic_link
        types = {
            str(stop.get("id")): stop.get("type")
            for stop in (link.itinerary_stops if link else [])
            if isinstance(stop, dict)
        }
        events = [
            e
            for e in (link.reach_events if link else [])
            if start <= _aware(e.reached_at) <= at
        ]
        pickups = [
            _aware(e.reached_at) for e in events if types.get(e.stop_id) == "pickup"
        ]
        returns = [
            _aware(e.reached_at) for e in events if types.get(e.stop_id) == "trip_end"
        ]
        volume = max(0, booking.num_passengers or 0)
        if pickups:
            returned = any(moment >= min(pickups) for moment in returns)
            if not returned:
                seats += volume
                if booking.status == BookingStatus.COMPLETED:
                    uncertain += volume
        elif start <= _aware(booking.created_at) <= at and (
            booking.trip_started_at
            or booking.started_at
            or booking.status == BookingStatus.COMPLETED
        ):
            uncertain += volume
    return seats, uncertain


def estimated_ashore_seats(db, *, company_id, period=None):
    return Figure(
        metric("estimated_ashore_seats"), _ashore_snapshot(db, company_id, period)[0]
    )


def ashore_uncertain_seats(db, *, company_id, period=None):
    return Figure(
        metric("ashore_uncertain_seats"), _ashore_snapshot(db, company_id, period)[1]
    )


COMPUTABLE_FIGURES.update(
    estimated_ashore_seats=estimated_ashore_seats,
    ashore_uncertain_seats=ashore_uncertain_seats,
)


def pending_transfers(db, *, company_id, period=None):
    from app.db.models.agency_transfer import AgencyTransfer
    from app.db.models.vessel_call import VesselCall

    query = (
        db.query(func.count(AgencyTransfer.id))
        .join(VesselCall, VesselCall.id == AgencyTransfer.vessel_call_id)
        .filter(
            AgencyTransfer.agency_id == VesselCall.agency_id,
            AgencyTransfer.status.notin_(("COMPLETED", "CANCELLED", "DECLINED")),
        )
    )
    query = tenure_scoped(
        db,
        query,
        company_id=company_id,
        vessel_column=VesselCall.vessel_id,
        occurred_column=AgencyTransfer.travel_at,
        period=period,
    )
    return Figure(metric("pending_transfers"), query.scalar() or 0)


COMPUTABLE_FIGURES["pending_transfers"] = pending_transfers
