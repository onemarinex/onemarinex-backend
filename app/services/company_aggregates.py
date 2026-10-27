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

Only the five metrics marked READY in the dictionary are computed here. The
labelled five are computable but need their caveat surfaced in the UI first;
the unavailable three have no source and must read "not available".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.db.models.cab_booking import CabBooking
from app.db.models.shipping_company import VesselRoleClaim
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
    period: Optional["Period"] = None,
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
            VesselRoleClaim.status == "APPROVED",
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.vessel_id == vessel_column,
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


def _suppressed(definition: Metric, value: int) -> Figure:
    """Apply the threshold once, in one place.

    Zero is never suppressed: withholding "none happened" tells the reader
    nothing and hides nothing.
    """
    if 0 < value < definition.suppression_threshold:
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
        db, query, company_id=company_id,
        vessel_column=vessel_column, occurred_column=occurred_column,
        period=period,
    )
    return _suppressed(definition, query.scalar() or 0)


# --- the five ready metrics --------------------------------------------------

def verified_vessels(db: Session, *, company_id: int, at: datetime) -> Figure:
    """Distinct vessels this company held at one instant.

    Distinct by vessel, not by claim: a company holding Owner and ISM on one
    ship is one vessel. Section 22 names that double count directly.
    """
    definition = metric("verified_vessels")
    moment = _aware(at)
    value = (
        db.query(func.count(func.distinct(VesselRoleClaim.vessel_id)))
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

    return _counted(
        db, metric_key="qualifying_calls", company_id=company_id, period=period,
        model=VesselCall, occurred_column=VesselCall.started_at,
        vessel_column=VesselCall.vessel_id,
    )


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

    definition = metric("incidents_reported")
    occurrence = func.coalesce(Incident.occurred_at, Incident.created_at)
    query = db.query(func.count(func.distinct(Incident.id)))
    query = tenure_scoped(
        db, query, company_id=company_id,
        vessel_column=Incident.vessel_id, occurred_column=occurrence,
        period=period,
    )
    return _suppressed(definition, query.scalar() or 0)


def incidents_resolved(db: Session, *, company_id: int, period: Optional['Period'] = None) -> Figure:
    """Resolution is its own event, at its own time.

    An incident raised under one company and resolved under the next counts
    here for whoever held the vessel when it was resolved, so this and
    `incidents_reported` need not reconcile -- and a ratio of the two is
    misleading across a handover, which the caveat states.
    """
    from app.db.models.incident import Incident

    definition = metric("incidents_resolved")
    query = db.query(func.count(func.distinct(Incident.id)))
    query = tenure_scoped(
        db, query, company_id=company_id,
        vessel_column=Incident.vessel_id, occurred_column=Incident.resolved_at,
        period=period,
    )
    return _suppressed(definition, query.scalar() or 0)


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

COMPUTABLE_FIGURES = {**READY_FIGURES, **LABELLED_FIGURES}
