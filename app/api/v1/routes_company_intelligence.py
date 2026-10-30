"""Company operational views. Exact call context and event-time authorization."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, func, literal, cast, String, DateTime, select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.shipping_company import VesselRoleClaim
from app.db.models.vessel_call import VesselCall
from app.db.models.port import Port
from app.db.models.port_rule import PortRule
from app.db.models.port_geography import PortGeography
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.agency_transfer import AgencyTransfer
from app.db.models.crew_assignment import CrewAssignment
from app.services.company_access import covering_claim
from app.services.company_fleet_identity import claim_matches_vessel, identity_key
from app.api.v1.routes_shipping_company import current_company_member
from app.services.company_aggregates import tenure_scoped, _ashore_snapshot, Period
from app.services.event_origin import resolved_origin
from app.services.vessel_lifecycle import current_call_predicate, call_is_current

router = APIRouter()
UTC = timezone.utc
PORTAL_METRIC_VERSION = "2026.10.28.portal.2"


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def reporting_window(period_start=None, period_end=None):
    if period_start is None and period_end is None:
        return None
    if period_start is None or period_end is None:
        raise HTTPException(422, "Provide both period_start and period_end.")
    if period_start.tzinfo is None or period_end.tzinfo is None or period_end <= period_start:
        raise HTTPException(422, "Use an increasing reporting period with explicit time zones.")
    return Period(period_start.astimezone(UTC), period_end.astimezone(UTC))


def filter_calls(query, *, period=None, port_id=None, vessel_id=None, country=None):
    if port_id is not None:
        query = query.filter(VesselCall.port_id == port_id)
    if vessel_id is not None:
        query = query.filter(VesselCall.vessel_id == vessel_id)
    if country and country.upper() != "ALL":
        code = country.strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise HTTPException(422, "Use a two-letter country code.")
        query = query.filter(VesselCall.port_id.in_(
            select(PortGeography.port_id).where(PortGeography.country_code == code)))
    if period:
        query = query.filter(VesselCall.started_at < period.end,
            func.coalesce(VesselCall.ended_at, VesselCall.etd, datetime.now(UTC)) > period.start)
    return query


def calls_query(db, company_id, active=False):
    now = datetime.now(UTC)
    claims = db.query(VesselRoleClaim.id).filter(
        VesselRoleClaim.company_id == company_id,
        claim_matches_vessel(VesselCall.vessel_id),
        VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED")),
        VesselRoleClaim.revoked_at.is_(None),
        VesselRoleClaim.effective_start < func.least(func.coalesce(VesselCall.ended_at, now), func.coalesce(VesselCall.etd, now), now),
        or_(
            VesselRoleClaim.effective_end.is_(None),
            VesselRoleClaim.effective_end > VesselCall.started_at,
        ),
    )
    if active:
        claims = claims.filter(
            VesselRoleClaim.effective_start <= now,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > now,
            ),
        )
    query = db.query(VesselCall).filter(claims.exists(), VesselCall.started_at <= now)
    if active:
        query = query.filter(current_call_predicate(now))
    return query


def selected_call(db, company_id, call_id):
    call = calls_query(db, company_id).filter(VesselCall.id == call_id).first()
    if call is None:
        raise HTTPException(404, "Port call not found")
    return call


def booking_query(
    db, company_id, call, timestamp, period=None, *, include_declared=False
):
    query = db.query(CabBooking).filter(
        CabBooking.vessel_call_id == call.id,
        CabBooking.vessel_id == call.vessel_id,
        resolved_origin(CabBooking),
        CabBooking.status != BookingStatus.CANCELLED,
    )
    return tenure_scoped(
        db,
        query,
        company_id=company_id,
        vessel_column=CabBooking.vessel_id,
        occurred_column=timestamp,
        period=period,
        include_declared=include_declared,
    )


def call_metrics(db, company_id, call, reporting_period=None):
    now = datetime.now(UTC)
    end = min(aware(call.ended_at), now) if call.ended_at else now
    if reporting_period:
        end = min(end, aware(reporting_period.end) - timedelta(microseconds=1))
    period = Period(aware(call.started_at), end + timedelta(microseconds=1))
    seats, unknown = _ashore_snapshot(
        db, company_id, period, vessel_call_id=call.id, include_declared=True
    )
    started = booking_query(
        db,
        company_id,
        call,
        CabBooking.trip_started_at,
        reporting_period,
        include_declared=True,
    )
    return {
        "crew_ashore": seats if unknown == 0 else None,
        "ashore_recorded_seats": seats,
        "ashore_uncertain_seats": unknown,
        "active_rides": (
            booking_query(
                db, company_id, call, CabBooking.trip_started_at, include_declared=True
            )
            .filter(
                CabBooking.trip_started_at <= end,
                or_(
                    CabBooking.trip_completed_at.is_(None),
                    CabBooking.trip_completed_at > end,
                ),
            )
            .count()
        ),
        "crew_participation": started.filter(CabBooking.trip_started_at <= end)
        .with_entities(func.coalesce(func.sum(CabBooking.num_passengers), 0))
        .scalar(),
        "total_booked_rides": booking_query(
            db,
            company_id,
            call,
            CabBooking.created_at,
            reporting_period,
            include_declared=True,
        ).count(),
        "as_of": end,
        "participation_unit": "Passenger seats on started rides. Repeat trips count again.",
        "ashore_unit": "Tracked passenger seats. This is not a verified headcount.",
    }


def call_out(db, company_id, call, metrics=True, reporting_period=None):
    now = datetime.now(UTC)
    known_ends = [aware(value) for value in (call.ended_at, call.etd) if value is not None and aware(value) <= now]
    port = db.get(Port, call.port_id) if call.port_id else None
    rule = (
        db.query(PortRule)
        .filter(PortRule.port_name.in_([port.code, port.name]))
        .first()
        if port
        else None
    )
    agency = call.agency_name
    if not agency and call.agency is not None:
        agency = call.agency.agency_name
    if agency and agency.strip().lower() == "other":
        agency = "Other / Not specified"
    out = {
        "id": call.id,
        "vessel_call_id": call.id,
        "vessel_id": call.vessel_id,
        "vessel_name": call.vessel_name,
        "imo_number": call.imo_number,
        "port_id": call.port_id,
        "port_name": (port.name if port else call.port_name) or "Port not recorded",
        "service_active": bool(port and port.is_active),
        "agent": agency or "Not assigned",
        "eta": call.eta,
        "etd": call.etd,
        "started_at": call.started_at,
        "ended_at": call.ended_at,
        "effective_ended_at": min(known_ends) if known_ends else None,
        "status": call.status if call_is_current(call) else (str(call.status).upper() if str(call.status).upper() in {"ARCHIVED", "REASSIGNED"} else "DEPARTED"),
        "current_port_call": call_is_current(call),
        "timezone": rule.timezone if rule and rule.timezone else "UTC",
        "timezone_recorded": bool(rule and rule.timezone),
    }
    if metrics:
        out["metrics"] = call_metrics(db, company_id, call, reporting_period)
    return out


def call_metrics_many(db, company_id, calls, reporting_period=None):
    """Skip per-call reducers only where there are no source booking rows.

    The existence read cannot release data: it only chooses whether to run the
    normal tenure-scoped reducers. Calls with any booking retain those checks.
    """
    calls = list(calls)
    with_bookings = {row[0] for row in db.query(CabBooking.vessel_call_id).filter(
        CabBooking.vessel_call_id.in_([c.id for c in calls])).distinct()}
    result = {}
    now = datetime.now(UTC)
    for call in calls:
        if call.id in with_bookings:
            result[call.id] = call_metrics(db, company_id, call, reporting_period)
            continue
        end = min(aware(call.ended_at), now) if call.ended_at else now
        if reporting_period:
            end = min(end, aware(reporting_period.end) - timedelta(microseconds=1))
        result[call.id] = {"crew_ashore": 0, "ashore_recorded_seats": 0,
            "ashore_uncertain_seats": 0, "active_rides": 0, "crew_participation": 0,
            "total_booked_rides": 0, "as_of": end,
            "participation_unit": "Passenger seats on started rides. Repeat trips count again.",
            "ashore_unit": "Tracked passenger seats. This is not a verified headcount."}
    return result


@router.get("/overview")
def overview(period_start: Optional[datetime] = None, period_end: Optional[datetime] = None,
    port_id: Optional[int] = None, vessel_id: Optional[int] = None, country: Optional[str] = None,
    db: Session = Depends(get_db), context=Depends(current_company_member)):
    company, _ = context
    now = datetime.now(UTC)
    onboarded = (
        db.query(func.count(func.distinct(identity_key())))
        .filter(
            VesselRoleClaim.company_id == company.id,
            VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED")),
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.effective_start <= now,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > now,
            ),
        )
        .scalar()
    )
    # Complete totals are independent of the paginated table endpoint.
    window = reporting_window(period_start, period_end)
    calls = filter_calls(calls_query(db, company.id, active=True), port_id=port_id, vessel_id=vessel_id, country=country).all()
    period_calls = filter_calls(calls_query(db, company.id), period=window, port_id=port_id, vessel_id=vessel_id, country=country).all()
    period_stats = call_metrics_many(db, company.id, period_calls, window)
    stats = [call_metrics(db, company.id, call) for call in calls]
    return {
        "generated_at": now,
        "metric_version": PORTAL_METRIC_VERSION,
        "company_name": company.legal_name,
        "reporting_period": {"start": window.start, "end": window.end} if window else None,
        "period_metrics": {"recorded_calls": len(period_calls),
            "recorded_vessels": len({c.vessel_id for c in period_calls}),
            "total_booked_rides": sum(m["total_booked_rides"] for m in period_stats.values()),
            "recorded_shore_leave_passengers": sum(m["crew_participation"] for m in period_stats.values())},
        "metrics": {
            "onboarded_vessels": onboarded,
            "active_vessels": len({c.vessel_id for c in calls}),
            "active_ports": len({c.port_id for c in calls if c.port_id}),
            "crew_ashore": (
                None
                if any(s["crew_ashore"] is None for s in stats)
                else sum(s["crew_ashore"] for s in stats)
            ),
            "ongoing_rides": sum(s["active_rides"] for s in stats),
        },
    }


@router.get("/calls")
def calls(
    port_id: int = None,
    vessel_id: int = None,
    country: Optional[str] = None,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
    active: bool = True,
    search: str = Query("", max_length=120),
    view: str = Query("active", pattern="^(active|ashore|rides)$"),
    historical_only: bool = False,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, _ = context
    window = reporting_window(period_start, period_end)
    query = filter_calls(calls_query(db, company.id, active and not historical_only), period=window, port_id=port_id, vessel_id=vessel_id, country=country)
    if historical_only:
        query = query.filter(~current_call_predicate())
    if port_id is not None:
        query = query.filter(VesselCall.port_id == port_id)
    if vessel_id is not None:
        query = query.filter(VesselCall.vessel_id == vessel_id)
    if search.strip():
        term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(
            or_(
                VesselCall.vessel_name.ilike(term, escape="\\"),
                VesselCall.imo_number.ilike(term, escape="\\"),
            )
        )
    if view in ("ashore", "rides"):
        qualified = []
        for call in query.order_by(
            VesselCall.started_at.desc(), VesselCall.id.desc()
        ).yield_per(100):
            row = call_out(db, company.id, call, reporting_period=window)
            if (
                row["metrics"]["ashore_recorded_seats"]
                if view == "ashore"
                else row["metrics"]["active_rides"]
            ) > 0:
                qualified.append(row)
        return {
            "total": len(qualified),
            "offset": offset,
            "limit": limit,
            "items": qualified[offset : offset + limit],
        }
    return {
        "total": query.count(),
        "offset": offset,
        "limit": limit,
        "items": [
            call_out(db, company.id, c, reporting_period=window)
            for c in query.order_by(VesselCall.started_at.desc(), VesselCall.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        ],
    }


def port_rows(db, company_id, active=True, search="", offset=None, limit=None, *, period=None, port_id=None, vessel_id=None, country=None):
    query = (
        filter_calls(calls_query(db, company_id, active), period=period, port_id=port_id, vessel_id=vessel_id, country=country)
        .join(Port, Port.id == VesselCall.port_id)
    )
    if active:
        query = query.filter(Port.is_active.is_(True))
    if search.strip():
        term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(Port.name.ilike(term, escape="\\"))
    grouped = query.with_entities(
        Port.id, Port.name, func.count(func.distinct(VesselCall.vessel_id)), func.count(VesselCall.id)
    ).group_by(Port.id, Port.name)
    total = grouped.count()
    grouped = grouped.order_by(Port.name, Port.id)
    if offset is not None:
        grouped = grouped.offset(offset).limit(limit)
    rows = grouped.all()
    ids = [r[0] for r in rows]
    geography = {
        p.port_id: p.country_code
        for p in db.query(PortGeography).filter(PortGeography.port_id.in_(ids))
    }
    current_calls = (
        filter_calls(calls_query(db, company_id, True), port_id=port_id, vessel_id=vessel_id, country=country).filter(VesselCall.port_id.in_(ids)).all()
    )
    by_port = {pid: [] for pid in ids}
    metrics_by_call = call_metrics_many(db, company_id, current_calls, period)
    for call in current_calls:
        by_port[call.port_id].append((call, metrics_by_call[call.id]))
    result = []
    for pid, name, recorded_vessels, recorded_calls in rows:
        stats = by_port[pid]
        result.append(
            {
                "id": pid,
                "name": name,
                "country_code": geography.get(pid),
                "vessels": recorded_vessels,
                "recorded_vessels": recorded_vessels,
                "recorded_calls": recorded_calls,
                "current_calls": len(stats),
                "active_vessels": len({c.vessel_id for c, _ in stats}),
                "crew_ashore": (
                    None
                    if any(m["crew_ashore"] is None for _, m in stats)
                    else sum(m["crew_ashore"] for _, m in stats)
                ),
                "active_rides": sum(m["active_rides"] for _, m in stats),
                "scope": "active" if active else "recorded",
            }
        )
    return {
        "items": result,
        "total": total,
        "offset": offset or 0,
        "limit": limit or total,
    }


@router.get("/ports")
def ports(
    active: bool = True,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    # Retain the first implementation's list contract for existing callers.
    return port_rows(db, context[0].id, active)["items"]


@router.get("/ports/page")
def ports_page(
    period_start: Optional[datetime] = None, period_end: Optional[datetime] = None,
    port_id: Optional[int] = None, vessel_id: Optional[int] = None, country: Optional[str] = None,
    active: bool = True,
    search: str = Query("", max_length=120),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    return port_rows(db, context[0].id, active, search, offset, limit, period=reporting_window(period_start, period_end), port_id=port_id, vessel_id=vessel_id, country=country)


@router.get("/ports/{port_id}/overview")
def selected_port(port_id: int, historical: bool = False,
    period_start: Optional[datetime] = None, period_end: Optional[datetime] = None,
    vessel_id: Optional[int] = None, country: Optional[str] = None, db: Session = Depends(get_db), context=Depends(current_company_member)):
    company, _ = context
    window = reporting_window(period_start, period_end)
    query = filter_calls(calls_query(db, company.id, not historical), period=window, port_id=port_id, vessel_id=vessel_id, country=country)
    if historical:
        query = query.filter(~current_call_predicate())
    rows = query.all()
    if not rows:
        # An eligible port can have no calls in the chosen current/history view.
        if not calls_query(db, company.id).filter(VesselCall.port_id == port_id).first():
            raise HTTPException(404, "Port not found.")
    port = db.get(Port, port_id)
    geography = db.get(PortGeography, port_id)
    values = list(call_metrics_many(db, company.id, rows, window).values())
    return {"id":port_id,"name":port.name if port else "Port not recorded","country_code":geography.country_code if geography else None,
        "generated_at":datetime.now(UTC),"metrics":{"vessels":len({call.vessel_id for call in rows}),"calls":len(rows),
            "crew_ashore":None if any(m["crew_ashore"] is None for m in values) else sum(m["crew_ashore"] for m in values),
            "active_rides":sum(m["active_rides"] for m in values),"total_booked_rides":sum(m["total_booked_rides"] for m in values)}}


@router.get("/calls/{call_id}")
def detail(
    call_id: int, period_start: Optional[datetime] = None, period_end: Optional[datetime] = None,
    db: Session = Depends(get_db), context=Depends(current_company_member)
):
    company, _ = context
    call = selected_call(db, company.id, call_id)
    result = call_out(db, company.id, call, reporting_period=reporting_window(period_start, period_end))
    result["generated_at"] = datetime.now(UTC)
    return result


@router.get("/calls/{call_id}/activities")
def activities(
    call_id: int,
    period_start: Optional[datetime] = None, period_end: Optional[datetime] = None,
    kind: str = Query("ALL", pattern="^(ALL|SIGN_ON|SIGN_OFF|SHORE_LEAVE)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, _ = context
    call = selected_call(db, company.id, call_id)
    window = reporting_window(period_start, period_end)
    empty_time = cast(literal(None), DateTime(timezone=True))
    parts = []
    if kind in ("ALL", "SIGN_ON", "SIGN_OFF"):
        transfers = (
            db.query(AgencyTransfer)
            .join(VesselCall, VesselCall.id == AgencyTransfer.vessel_call_id)
            .filter(
                AgencyTransfer.vessel_call_id == call.id,
                AgencyTransfer.agency_id == call.agency_id,
            )
        )
        transfers = tenure_scoped(
            db,
            transfers,
            company_id=company.id,
            vessel_column=VesselCall.vessel_id,
            occurred_column=AgencyTransfer.travel_at,
            period=window,
        )
        if kind != "ALL":
            transfers = transfers.filter(AgencyTransfer.movement_type == kind)
        parts.append(
            transfers.with_entities(
                literal("transport").label("record_type"),
                cast(AgencyTransfer.id, String).label("reference"),
                AgencyTransfer.movement_type.label("type"),
                AgencyTransfer.status.label("status"),
                AgencyTransfer.travel_at.label("occurred_at"),
                AgencyTransfer.started_at.label("started_at"),
                AgencyTransfer.completed_at.label("completed_at"),
            )
        )
        for movement, timestamp in (
            ("SIGN_ON", CrewAssignment.signed_on_at),
            ("SIGN_OFF", CrewAssignment.signed_off_at),
        ):
            if kind not in ("ALL", movement):
                continue
            confirmed = (
                db.query(CrewAssignment)
                .join(VesselCall, VesselCall.id == CrewAssignment.vessel_call_id)
                .filter(CrewAssignment.vessel_call_id == call.id, timestamp.isnot(None))
            )
            confirmed = tenure_scoped(
                db,
                confirmed,
                company_id=company.id,
                vessel_column=VesselCall.vessel_id,
                occurred_column=timestamp,
                period=window,
            )
            parts.append(
                confirmed.with_entities(
                    literal("confirmed_movement"),
                    cast(CrewAssignment.id, String),
                    literal(movement),
                    literal("CONFIRMED"),
                    timestamp,
                    empty_time,
                    empty_time,
                )
            )
    if kind in ("ALL", "SHORE_LEAVE"):
        bookings = booking_query(db, company.id, call, CabBooking.created_at, window)
        parts.append(
            bookings.with_entities(
                literal("shore_leave").label("record_type"),
                cast(CabBooking.booking_id, String).label("reference"),
                literal("SHORE_LEAVE").label("type"),
                cast(CabBooking.status, String).label("status"),
                CabBooking.created_at.label("occurred_at"),
                CabBooking.trip_started_at.label("started_at"),
                CabBooking.trip_completed_at.label("completed_at"),
            )
        )
    query = parts[0].union_all(*parts[1:]).subquery()
    total = db.query(func.count()).select_from(query).scalar()
    rows = (
        db.query(query)
        .order_by(query.c.occurred_at.desc(), query.c.record_type, query.c.reference)
        .offset(offset)
        .limit(limit)
        .all()
    )

    def permitted_time(value):
        return (
            value
            if value
            and covering_claim(
                db, company_id=company.id, vessel_id=call.vessel_id, occurred_at=value
            )
            else None
        )

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": f"{r.record_type}:{r.type}:{r.reference}",
                "type": r.type,
                "record_type": r.record_type,
                "status": r.status,
                "planned_at": r.occurred_at,
                "started_at": permitted_time(r.started_at),
                "completed_at": permitted_time(r.completed_at),
            }
            for r in rows
        ],
        "coverage": "Detailed activity requires an independently approved vessel relationship. Automatic acceptance permits aggregate figures only. Transport activity and confirmed movements are separate records.",
    }
