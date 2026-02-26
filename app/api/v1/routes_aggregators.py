from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional, Dict, Any
from datetime import datetime, date

from app.db.session import get_db
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.driver import Driver
from app.db.models.crew_profile import CrewProfile
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel, EmailStr

router = APIRouter()

# --- Schemas ---

class CrewShortOut(BaseModel):
    name: str
    hp_id: str
    vessel: str

class BookingDashboardOut(BaseModel):
    id: int
    booking_id: str
    crew: CrewShortOut
    pickup_address: str
    drop_address: str
    vehicle_name: str
    estimated_price: float
    status: str
    created_at: datetime
    scheduled_time: Optional[datetime]
    driver_name: Optional[str]
    num_passengers: int

class AggregatorDashboardData(BaseModel):
    stats: Dict[str, int]
    pending_requests: List[BookingDashboardOut]
    active_trips: List[BookingDashboardOut]

class DriverAssignIn(BaseModel):
    booking_id: str
    driver_id: int

class AggregatorProfileOut(BaseModel):
    id: int
    name: Optional[str]
    email: EmailStr
    mobile_number: Optional[str]
    company_name: str
    contact_person: Optional[str]
    operating_port: str
    gst_number: Optional[str]
    status: str
    profile_image: Optional[str]
    aggregator_identifier: Optional[str]

    class Config:
        from_attributes = True

# --- Routes ---

@router.get("/profile", response_model=AggregatorProfileOut)
def get_aggregator_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only aggregators can access this profile")
    
    agg_profile = current_user.aggregator_profile
    if not agg_profile:
        raise HTTPException(status_code=404, detail="Aggregator profile not found")
    
    return {
        "id": agg_profile.id,
        "name": current_user.name,
        "email": current_user.email,
        "mobile_number": current_user.mobile_number,
        "company_name": agg_profile.company_name,
        "contact_person": agg_profile.contact_person,
        "operating_port": agg_profile.operating_port,
        "gst_number": agg_profile.gst_number,
        "status": agg_profile.status,
        "profile_image": agg_profile.profile_image,
        "aggregator_identifier": agg_profile.aggregator_identifier
    }

@router.get("/dashboard", response_model=AggregatorDashboardData)
def get_aggregator_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only aggregators can access the dashboard")
    
    agg_profile = current_user.aggregator_profile
    if not agg_profile:
        raise HTTPException(status_code=404, detail="Aggregator profile not found")
    
    port = agg_profile.operating_port
    
    # Base query for aggregator's port
    query = db.query(CabBooking).filter(CabBooking.port.ilike(f"%{port}%"))
    
    # Stats
    pending_count = query.filter(CabBooking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED])).count()
    active_count = query.filter(CabBooking.status == BookingStatus.DRIVER_ASSIGNED).count()
    in_progress_count = query.filter(CabBooking.status == BookingStatus.IN_PROGRESS).count()
    
    today = date.today()
    completed_today = query.filter(
        CabBooking.status == BookingStatus.COMPLETED,
        func.date(CabBooking.updated_at) == today
    ).count()

    # Lists
    pending_bookings = query.filter(CabBooking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED])).order_by(CabBooking.created_at.desc()).all()
    active_bookings = query.filter(CabBooking.status.in_([BookingStatus.DRIVER_ASSIGNED, BookingStatus.IN_PROGRESS])).order_by(CabBooking.created_at.desc()).all()

    def transform(b: CabBooking):
        return BookingDashboardOut(
            id=b.id,
            booking_id=b.booking_id,
            crew=CrewShortOut(
                name=b.crew.full_name,
                hp_id=b.crew.passport_number or "",
                vessel=b.crew.vessel or ""
            ),
            pickup_address=b.pickup_address,
            drop_address=b.drop_address,
            vehicle_name=b.vehicle_name,
            estimated_price=float(b.estimated_price),
            status=b.status.value,
            created_at=b.created_at,
            scheduled_time=b.scheduled_time,
            driver_name=b.driver_name,
            num_passengers=b.num_passengers
        )

    return AggregatorDashboardData(
        stats={
            "pending_requests": pending_count,
            "active_trips": active_count,
            "in_progress_trips": in_progress_count,
            "today_completed_trips": completed_today
        },
        pending_requests=[transform(b) for b in pending_bookings],
        active_trips=[transform(b) for b in active_bookings]
    )

@router.post("/dashboard/assign-driver")
def assign_driver(
    body: DriverAssignIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    booking = db.query(CabBooking).filter(CabBooking.booking_id == body.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    driver = db.query(Driver).filter(Driver.id == body.driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    booking.driver_id = driver.id
    booking.driver_name = driver.name
    booking.driver_phone = driver.phone
    booking.driver_plate = driver.vehicle_number
    booking.aggregator_id = current_user.aggregator_profile.id
    booking.status = BookingStatus.DRIVER_ASSIGNED
    
    db.commit()
    return {"message": "Driver assigned successfully"}

@router.post("/dashboard/decline-ride")
def decline_ride(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    booking.status = BookingStatus.CANCELLED
    db.commit()
    return {"message": "Ride declined successfully"}
