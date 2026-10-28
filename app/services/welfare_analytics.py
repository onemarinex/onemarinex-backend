"""Operational participation and service reviews. No welfare score is inferred."""

from datetime import timezone
from sqlalchemy import or_, func
from fastapi import HTTPException
from sqlalchemy.orm import selectinload
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.booking_review import BookingReview
from app.db.models.driver_magic_link import DriverMagicLink
from app.db.models.vessel_call import VesselCall
from app.services.event_origin import resolved_origin
from app.services.company_aggregates import tenure_scoped
from app.services.company_access import covering_claim
from app.services.welfare_scope import filter_country


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def merged_minutes(intervals):
    total = 0
    left = right = None
    for start, end in sorted(intervals):
        if right is not None and start <= right:
            right = max(right, end)
        else:
            if right is not None:
                total += (right - left).total_seconds()
            left, right = start, end
    if right is not None:
        total += (right - left).total_seconds()
    return total / 60


def operational(
    db, start, end, company_id=None, port_id=None, vessel_id=None, country_code=None
):
    query = (
        db.query(CabBooking)
        .join(VesselCall, VesselCall.id == CabBooking.vessel_call_id)
        .filter(
            resolved_origin(CabBooking),
            CabBooking.status != BookingStatus.CANCELLED,
            CabBooking.trip_started_at >= start,
            CabBooking.trip_started_at < end,
        )
    )
    if port_id:
        query = query.filter(VesselCall.port_id == port_id)
    if vessel_id:
        query = query.filter(CabBooking.vessel_id == vessel_id)
    if company_id:
        query = tenure_scoped(
            db,
            query,
            company_id=company_id,
            vessel_column=CabBooking.vessel_id,
            occurred_column=CabBooking.trip_started_at,
        )
    query = filter_country(query, VesselCall.port_id, country_code)
    query = query.options(
        selectinload(CabBooking.magic_link).selectinload(DriverMagicLink.reach_events)
    )
    groups = {}
    seats = 0
    call_ids = set()
    spans = {}
    unfinished = set()
    facilities = 0
    for b in query.yield_per(500):
        call = db.get(VesselCall, b.vessel_call_id)
        volume = max(0, b.num_passengers or 0)
        seats += volume
        call_ids.add(call.id)
        key = (call.port_id, call.vessel_id, call.id)
        row = groups.setdefault(
            key,
            {
                "call_id": call.id,
                "vessel_id": call.vessel_id,
                "port_id": call.port_id,
                "vessel_name": call.vessel_name,
                "port_name": call.port_name,
                "participation": 0,
                "rides": 0,
            },
        )
        row["participation"] += volume
        row["rides"] += 1
        link = b.magic_link
        if not link:
            continue
        types = {
            str(s.get("id")): s.get("type")
            for s in link.itinerary_stops or []
            if isinstance(s, dict)
        }
        events = [
            e
            for e in link.reach_events
            if start <= aware(e.reached_at) < end
            and (
                not company_id
                or covering_claim(
                    db,
                    company_id=company_id,
                    vessel_id=call.vessel_id,
                    occurred_at=e.reached_at,
                )
            )
        ]
        facilities += sum(types.get(e.stop_id) == "facility" for e in events)
        pickups = sorted(
            aware(e.reached_at) for e in events if types.get(e.stop_id) == "pickup"
        )
        returns = sorted(
            aware(e.reached_at) for e in events if types.get(e.stop_id) == "trip_end"
        )
        person = (
            ("profile", b.crew_id)
            if b.crew_id
            else ("assignment", b.crew_assignment_id) if b.crew_assignment_id else None
        )
        if not person:
            continue
        if pickups:
            finish = next((t for t in returns if t >= pickups[0]), None)
            if finish:
                spans.setdefault(person, []).append((pickups[0], finish))
            else:
                unfinished.add(person)
    minutes = [
        merged_minutes(s) for person, s in spans.items() if person not in unfinished
    ]
    if company_id:
        from app.api.v1.routes_company_intelligence import calls_query

        call_query = calls_query(db, company_id)
    else:
        call_query = db.query(VesselCall)
    call_query = call_query.filter(
        VesselCall.started_at < end,
        or_(VesselCall.ended_at.is_(None), VesselCall.ended_at > start),
    )
    if port_id:
        call_query = call_query.filter(VesselCall.port_id == port_id)
    if vessel_id:
        call_query = call_query.filter(VesselCall.vessel_id == vessel_id)
    call_query = filter_country(call_query, VesselCall.port_id, country_code)
    recorded = call_query.count()
    private_duration = bool(company_id and len(minutes) < 5)
    return {
        "recorded_calls": recorded,
        "calls_with_shore_leave_share": (
            len(call_ids) * 100 / recorded if recorded else None
        ),
        "crew_participation": seats,
        "participation_unit": "Passenger seats on commenced rides. Repeat passengers can count again.",
        "calls_with_shore_leave": len(call_ids),
        "average_time_ashore_minutes": (
            sum(minutes) / len(minutes) if minutes and not private_duration else None
        ),
        "duration_sample_people": None if private_duration else len(minutes),
        "duration_coverage": "Recorded complete pickup-to-port intervals for linked people. Overlaps are merged. Unfinished and unlinked intervals are excluded.",
        "facilities_visited": facilities,
        "welfare_service_bookings": None,
        "service_coverage": "No approved service-category mapping is recorded. Reached facilities are not completed service orders.",
        "calls": list(groups.values()),
    }


def reviews_query(
    db,
    start,
    end,
    company_id=None,
    port_id=None,
    vessel_id=None,
    country_code=None,
    review_type=None,
    provider_id=None,
):
    query = (
        db.query(BookingReview)
        .join(CabBooking, CabBooking.id == BookingReview.booking_id)
        .join(VesselCall, VesselCall.id == CabBooking.vessel_call_id)
        .filter(
            resolved_origin(CabBooking),
            BookingReview.created_at >= start,
            BookingReview.created_at < end,
        )
    )
    if port_id:
        query = query.filter(VesselCall.port_id == port_id)
    if vessel_id:
        query = query.filter(CabBooking.vessel_id == vessel_id)
    if company_id:
        query = tenure_scoped(
            db,
            query,
            company_id=company_id,
            vessel_column=CabBooking.vessel_id,
            occurred_column=BookingReview.created_at,
        )
    query = filter_country(query, VesselCall.port_id, country_code)
    if review_type:
        if review_type not in ("driver", "facility_stop"):
            raise HTTPException(422, "Choose driver or facility_stop reviews.")
        query = query.filter(BookingReview.review_type == review_type)
    if provider_id is not None:
        query = query.filter(
            func.coalesce(CabBooking.provider_id, CabBooking.aggregator_id)
            == provider_id
        )
    return query


def review_summary(
    db,
    start,
    end,
    company_id=None,
    port_id=None,
    vessel_id=None,
    country_code=None,
    review_type=None,
    provider_id=None,
):
    query = reviews_query(
        db,
        start,
        end,
        company_id,
        port_id,
        vessel_id,
        country_code,
        review_type,
        provider_id,
    )
    groups = {}
    for r in query.yield_per(500):
        groups.setdefault(r.review_type, []).append(r)
    result = []
    for kind, rows in sorted(groups.items()):
        people = {r.crew_id for r in rows}
        suppressed = bool(company_id and len(people) < 5)
        distribution = []
        for rating in range(1, 6):
            matches = [r for r in rows if round(r.rating) == rating]
            hidden = bool(company_id and len({r.crew_id for r in matches}) < 5)
            distribution.append(
                {
                    "rating": rating,
                    "count": None if suppressed or hidden else len(matches),
                }
            )
        # Do not reveal a small cell by subtracting the other stars from the total.
        if company_id and any(d["count"] is None for d in distribution):
            distribution = [{"rating": rating, "count": None} for rating in range(1, 6)]
        result.append(
            {
                "type": kind,
                "suppressed": suppressed,
                "reviews": None if suppressed else len(rows),
                "respondents": None if suppressed else len(people),
                "average_rating": (
                    None if suppressed else sum(r.rating for r in rows) / len(rows)
                ),
                "distribution": distribution,
            }
        )
    return {
        "groups": result,
        "coverage": "Driver and facility star ratings. These are not overall shore-leave satisfaction scores. Company detail requires a separate approved disclosure.",
    }
