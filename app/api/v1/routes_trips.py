from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.db.session import get_db
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.crew_profile import CrewProfile
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel

router = APIRouter()

class TripCrewOut(BaseModel):
    name: str
    rank: str
    hp_id: str

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

@router.get("/monitoring", response_model=MonitoringResponse)
def get_trip_monitoring(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only agents can access trip monitoring"
        )

    # Get crew profile IDs for this agent's vessels
    vessel_ids = [v.id for v in db.query(Vessel).filter(Vessel.agent_id == current_user.id).all()]
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

    if not crew_profile_ids:
        return MonitoringResponse(ongoing=[], requested=[], completed=[])

    # Use raw SQL to avoid SQLEnum deserialization errors from inconsistent DB data
    placeholders = ",".join([str(pid) for pid in crew_profile_ids])
    rows = db.execute(text(f"""
        SELECT cb.id, cb.booking_id, cb.pickup_address, cb.drop_address,
               cb.pickup_lat, cb.pickup_lng, cb.drop_lat, cb.drop_lng,
               cb.vehicle_name, cb.estimated_price, cb.driver_name,
               cb.driver_phone, cb.driver_plate, cb.aggregator_name,
               cb.status, cb.created_at, cb.scheduled_time,
               cp.full_name, cp.rank, cp.hpid
        FROM cab_bookings cb
        JOIN crew_profiles cp ON cb.crew_id = cp.id
        WHERE cb.crew_id IN ({placeholders})
    """)).all()

    ongoing = []
    requested = []
    completed = []

    active_statuses = {"in_progress", "driver_assigned", "driver_accepted"}
    requested_statuses = {"pending", "confirmed", "pending_provider_response", "provider_accepted"}

    for row in rows:
        status_val = (row.status or "").lower()

        crew_details = TripCrewOut(
            name=row.full_name,
            rank=row.rank,
            hp_id=row.hpid or ""
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
            scheduled_time=row.scheduled_time
        )

        if status_val in active_statuses:
            ongoing.append(trip)
        elif status_val in requested_statuses:
            requested.append(trip)
        elif status_val == "completed":
            completed.append(trip)

    return MonitoringResponse(
        ongoing=ongoing,
        requested=requested,
        completed=completed
    )
