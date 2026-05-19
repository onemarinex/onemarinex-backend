from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.db.session import get_db
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.crew_profile import CrewProfile
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

from typing import Optional

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

    # Fetch all bookings
    # In a real app, we might filter by agent_id if bookings were linked to agents
    # For now, we fetch all as per the requirement "trips all the trips are there in cab bookings table"
    bookings = db.query(CabBooking).join(CrewProfile).all()

    ongoing = []
    requested = []
    completed = []

    for b in bookings:
        crew_details = TripCrewOut(
            name=b.crew.full_name,
            rank=b.crew.rank,
            hp_id=b.crew.hpid or ""
        )
        
        trip = TripDetailsOut(
            id=b.id,
            booking_id=b.booking_id,
            crew_details=crew_details,
            pickup_address=b.pickup_address,
            drop_address=b.drop_address,
            pickup_lat=b.pickup_lat,
            pickup_lng=b.pickup_lng,
            drop_lat=b.drop_lat,
            drop_lng=b.drop_lng,
            vehicle_name=b.vehicle_name,
            estimated_price=float(b.estimated_price),
            driver_name=b.driver_name,
            driver_phone=b.driver_phone,
            driver_plate=b.driver_plate,
            aggregator_name=b.aggregator_name,
            status=b.status.value,
            created_at=b.created_at,
            scheduled_time=b.scheduled_time
        )

        if b.status in [BookingStatus.IN_PROGRESS, BookingStatus.DRIVER_ASSIGNED]:
            ongoing.append(trip)
        elif b.status in [BookingStatus.PENDING, BookingStatus.CONFIRMED]:
            requested.append(trip)
        elif b.status == BookingStatus.COMPLETED:
            completed.append(trip)
        # Cancelled trips are ignored in monitoring for now based on screenshots

    return MonitoringResponse(
        ongoing=ongoing,
        requested=requested,
        completed=completed
    )
