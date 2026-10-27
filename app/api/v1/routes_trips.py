import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.db.session import get_db
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.crew_profile import CrewProfile
from app.db.models.agent_profile import AgentProfile
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from app.services.vessel_lifecycle import current_call_for
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)

class TripCrewOut(BaseModel):
    name: str
    rank: str
    hp_id: str
    # The opaque per-assignment reference. Preferred for display over hp_id,
    # which in the legacy format embeds the passport number. Empty where the
    # trip predates the reference or carries no assignment.
    reference: str

class TripDetailsOut(BaseModel):
    id: int
    booking_id: str
    crew_details: TripCrewOut
    pickup_address: str
    drop_address: str
    pickup_lat: float
    pickup_lng: float
    drop_lat: float
    drop_lng: float
    vehicle_name: str
    estimated_price: float
    # Present so the agent's Trips table can carry the same columns as the
    # superadmin bookings page.
    port: Optional[str] = None
    ride_type: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    driver_plate: Optional[str] = None
    aggregator_name: Optional[str] = None
    status: str
    created_at: datetime
    scheduled_time: Optional[datetime] = None

    class Config:
        from_attributes = True

class MonitoringResponse(BaseModel):
    ongoing: List[TripDetailsOut]
    requested: List[TripDetailsOut]
    completed: List[TripDetailsOut]

def _agent_trip_scope(db: Session, agent_user_id: int):
    """Vessels this agent operates and the crew profiles sailing on them."""
    vessel_ids = [v.id for v in db.query(Vessel).filter(Vessel.agent_id == agent_user_id).all()]
    if not vessel_ids:
        return [], []
    hpids = [
        c.hp_id for c in db.query(VesselCrew).filter(
            VesselCrew.vessel_id.in_(vessel_ids), VesselCrew.hp_id.isnot(None)
        ).all() if c.hp_id
    ]
    if not hpids:
        return vessel_ids, []
    crew_profile_ids = [
        cp.id for cp in db.query(CrewProfile).filter(CrewProfile.hpid.in_(hpids)).all()
    ]
    return vessel_ids, crew_profile_ids


def _agent_owns_trip(db: Session, agent_user_id: int, booking: CabBooking) -> bool:
    agency_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == agent_user_id
    ).scalar()
    if booking.agency_id is not None:
        return agency_id is not None and booking.agency_id == agency_id
    vessel_ids, crew_profile_ids = _agent_trip_scope(db, agent_user_id)
    if booking.vessel_id is not None:
        return booking.vessel_id in vessel_ids
    return booking.crew_id is not None and booking.crew_id in crew_profile_ids


@router.get("/monitoring", response_model=MonitoringResponse)
def get_trip_monitoring(
    vessel_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Trips for the agent's crew, optionally narrowed to one ship.

    `vessel_id` backs the Trips section on Vessel Details. It is checked against
    the agent's own vessels and 404s otherwise, so it cannot be used to read
    another agency's trips by guessing an id.
    """
    if current_user.role != "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only agents can access trip monitoring"
        )

    # Get crew profile IDs for this agent's vessels
    vessel_ids = [v.id for v in db.query(Vessel).filter(Vessel.agent_id == current_user.id).all()]
    call = None
    if vessel_id is not None:
        if vessel_id not in vessel_ids:
            raise HTTPException(status_code=404, detail="Vessel not found")
        vessel_ids = [vessel_id]
        # One ship means one call: the page asking for it is showing the call
        # the vessel is on. A returning vessel was carrying its previous calls'
        # trips into the new one.
        call = current_call_for(db, vessel_id)

    crew_hpids = []
    if vessel_ids:
        crew_hpids = [
            c.hp_id for c in db.query(VesselCrew).filter(
                VesselCrew.vessel_id.in_(vessel_ids),
                VesselCrew.hp_id.isnot(None),
            ).all()
            if c.hp_id
        ]

    crew_profile_ids = []
    if crew_hpids:
        crew_profile_ids = [
            cp.id for cp in db.query(CrewProfile).filter(CrewProfile.hpid.in_(crew_hpids)).all()
        ]

    if not vessel_ids:
        return MonitoringResponse(ongoing=[], requested=[], completed=[])

    # Use raw SQL to avoid SQLEnum deserialization errors from inconsistent DB data
    vessel_placeholders = ",".join([str(value) for value in vessel_ids])
    legacy_crew_clause = "FALSE"
    if crew_profile_ids:
        crew_placeholders = ",".join([str(value) for value in crew_profile_ids])
        legacy_crew_clause = f"cb.crew_id IN ({crew_placeholders})"
    agency_profile_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == current_user.id
    ).scalar()
    strong_clause = "FALSE"
    if agency_profile_id is not None:
        strong_clause = (
            f"cb.agency_id = {int(agency_profile_id)} "
            f"AND cb.vessel_id IN ({vessel_placeholders})"
        )
    # Trips of this call only. Rows stamped with another call are out; rows from
    # before the column existed carry no call and are admitted only if they ran
    # inside this call's window.
    call_clause = "TRUE"
    call_params: dict = {}
    if call is not None:
        unattributed = ["cb.vessel_call_id IS NULL"]
        started = call.started_at or call.created_at
        if started is not None:
            unattributed.append("cb.created_at >= :call_started")
            call_params["call_started"] = started
        if call.ended_at is not None:
            unattributed.append("cb.created_at <= :call_ended")
            call_params["call_ended"] = call.ended_at
        call_clause = (
            f"(cb.vessel_call_id = {int(call.id)} OR ({' AND '.join(unattributed)}))"
        )

    rows = db.execute(text(f"""
        SELECT cb.id, cb.booking_id, cb.pickup_address, cb.drop_address,
               cb.pickup_lat, cb.pickup_lng, cb.drop_lat, cb.drop_lng,
               cb.vehicle_name, cb.estimated_price, cb.driver_name,
               cb.driver_phone, cb.driver_plate, cb.aggregator_name,
               cb.status, cb.created_at, cb.scheduled_time, cb.port, cb.ride_type,
               COALESCE(cp.full_name, ca.crew_name, 'Historical crew') AS full_name,
               COALESCE(cp.rank, ca.rank, 'Unknown') AS rank,
               COALESCE(cp.hpid, ca.hpid, '') AS hpid,
               COALESCE(ca.public_ref, '') AS reference
        FROM cab_bookings cb
        LEFT JOIN crew_profiles cp ON cb.crew_id = cp.id
        LEFT JOIN crew_assignments ca ON cb.crew_assignment_id = ca.id
        WHERE (
            ({strong_clause})
            OR (
                cb.agency_id IS NULL
                AND (
                    cb.vessel_id IN ({vessel_placeholders})
                    OR (cb.vessel_id IS NULL AND {legacy_crew_clause})
                )
            )
        )
        AND {call_clause}
        ORDER BY cb.created_at DESC, cb.id DESC
    """), call_params).all()

    ongoing = []
    requested = []
    completed = []

    # Buckets follow the driver's own lifecycle, which is what the agent sees:
    #   POST /drivers/rides/{id}/accept    -> driver_accepted
    #   POST /drivers/rides/{id}/arrive    -> arrived    <- "reached pickup point"
    #   POST /drivers/rides/{id}/start     -> on_trip
    #   POST /drivers/rides/{id}/complete  -> completed  <- "ride completed"
    #
    # So a trip is "requested" from the moment it is raised until the driver
    # reaches the pickup point, and "on going" from that point until completed.
    # driver_assigned/driver_accepted mean a driver is on the way, not that the
    # trip has started, so they belong in requested.
    requested_statuses = {
        "pending", "confirmed", "pending_provider_response", "provider_accepted",
        "driver_assigned", "driver_accepted",
    }
    # "in_progress" is the legacy spelling of on_trip, still on older rows.
    active_statuses = {"arrived", "on_trip", "in_progress"}
    completed_statuses = {"completed"}
    # Deliberately shown in no tab; the screen has no place for them.
    ignored_statuses = {"cancelled", "provider_rejected"}

    for row in rows:
        status_val = (row.status or "").lower()

        crew_details = TripCrewOut(
            name=row.full_name,
            rank=row.rank,
            hp_id=row.hpid or "",
            reference=row.reference or "",
        )

        trip = TripDetailsOut(
            id=row.id,
            booking_id=row.booking_id,
            crew_details=crew_details,
            pickup_address=row.pickup_address,
            drop_address=row.drop_address,
            pickup_lat=row.pickup_lat,
            pickup_lng=row.pickup_lng,
            drop_lat=row.drop_lat,
            drop_lng=row.drop_lng,
            vehicle_name=row.vehicle_name,
            estimated_price=float(row.estimated_price) if row.estimated_price else 0,
            driver_name=row.driver_name,
            driver_phone=row.driver_phone,
            driver_plate=row.driver_plate,
            aggregator_name=row.aggregator_name,
            status=status_val,
            created_at=row.created_at,
            scheduled_time=row.scheduled_time,
            port=row.port,
            ride_type=row.ride_type,
        )

        if status_val in active_statuses:
            ongoing.append(trip)
        elif status_val in requested_statuses:
            requested.append(trip)
        elif status_val in completed_statuses:
            completed.append(trip)
        elif status_val not in ignored_statuses:
            # An unmapped status used to fall through here and disappear from
            # every tab, which is how live arrived/on_trip rides became
            # invisible. Surface it as requested and say so in the log.
            logger.warning(
                "Trip %s has unmapped status %r; showing it under Requested.",
                row.booking_id, status_val,
            )
            requested.append(trip)

    return MonitoringResponse(
        ongoing=ongoing,
        requested=requested,
        completed=completed
    )


class TripActivityStopOut(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    type: Optional[str] = None
    reached: bool = False
    reached_at: Optional[datetime] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class TripActivityOut(BaseModel):
    booking_id: str
    booking_status: Optional[str] = None
    otp_verified: bool = False
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    driver_plate: Optional[str] = None
    stops: List[TripActivityStopOut] = Field(default_factory=list)
    stops_reached: int = 0
    stops_total: int = 0
    last_activity_at: Optional[datetime] = None
    tracking_available: bool = False
    last_reached_point: Optional[TripActivityStopOut] = None
    next_destination: Optional[TripActivityStopOut] = None


@router.get("/{booking_id}/activity", response_model=TripActivityOut)
def get_trip_activity(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Live crew activity for one trip — the same progress the driver's magic
    link shows, surfaced to the agent.

    The magic link is public and keyed on an unguessable token; this route is
    the agent-facing equivalent, so it is scoped to the agent's own crew rather
    than being open to anyone with a booking id.
    """
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can view trip activity")

    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Trip not found")

    if not _agent_owns_trip(db, current_user.id, booking):
        # Same shape as "not found", so an agent cannot probe for other
        # agencies' booking ids.
        raise HTTPException(status_code=404, detail="Trip not found")

    from app.db.models.driver_magic_link import DriverMagicLink
    from app.services.magic_link_service import serialize_magic_link_public_payload

    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.booking_id == booking.id)
        .order_by(DriverMagicLink.id.desc())
        .first()
    )

    if not magic_link:
        # No driver link yet: the trip exists but nothing is being tracked.
        return TripActivityOut(
            booking_id=booking.booking_id,
            booking_status=booking.status.value if hasattr(booking.status, "value") else str(booking.status),
            driver_name=booking.driver_name,
            driver_phone=booking.driver_phone,
            driver_plate=booking.driver_plate,
            tracking_available=False,
        )

    payload = serialize_magic_link_public_payload(magic_link)
    # The driver's public payload calls the planned stop collection
    # ``itinerary``. Reuse it directly so the agent and driver views cannot
    # disagree about reached state or stop order.
    stops = payload.get("itinerary") or []
    reached = [s for s in stops if s.get("reached")]
    last_at = max((s.get("reached_at") for s in reached if s.get("reached_at")), default=None)
    reached_ordered = sorted(
        reached,
        key=lambda item: str(item.get("reached_at") or ""),
    )
    last_reached = reached_ordered[-1] if reached_ordered else None
    next_destination = next((item for item in stops if not item.get("reached")), None)

    def stop_out(item):
        if not item:
            return None
        return TripActivityStopOut(
            id=str(item.get("id")) if item.get("id") is not None else None,
            name=item.get("name"), address=item.get("address"), type=item.get("type"),
            reached=bool(item.get("reached")), reached_at=item.get("reached_at"),
            lat=(item.get("reached_location") or {}).get("lat") or item.get("lat"),
            lng=(item.get("reached_location") or {}).get("lng") or item.get("lng"),
        )

    return TripActivityOut(
        booking_id=booking.booking_id,
        booking_status=payload.get("booking_status"),
        otp_verified=bool(payload.get("otp_verified")),
        driver_name=booking.driver_name,
        driver_phone=booking.driver_phone,
        driver_plate=booking.driver_plate,
        stops=[
            TripActivityStopOut(
                id=str(s.get("id")) if s.get("id") is not None else None,
                name=s.get("name"),
                address=s.get("address"),
                type=s.get("type"),
                reached=bool(s.get("reached")),
                reached_at=s.get("reached_at"),
                lat=(s.get("reached_location") or {}).get("lat") or s.get("lat"),
                lng=(s.get("reached_location") or {}).get("lng") or s.get("lng"),
            )
            for s in stops
        ],
        stops_reached=len(reached),
        stops_total=len(stops),
        last_activity_at=last_at,
        tracking_available=True,
        last_reached_point=stop_out(last_reached),
        next_destination=stop_out(next_destination),
    )
