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
from app.db.models.pricing_controls import PricingDuration, PricingRideType, PricingRule, PricingVehicleCategory
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel, EmailStr

router = APIRouter()

PRICING_PROVIDER_TYPE_BY_PROFILE_TYPE = {
    "partnered_driver": "partner_drivers",
    "aggregator": "aggregators",
}

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
    completed_requests: List[BookingDashboardOut]

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
        "operating_port": agg_profile.operating_port.name,
        "gst_number": agg_profile.gst_number,
        "status": agg_profile.status,
        "profile_image": agg_profile.profile_image,
        "aggregator_identifier": agg_profile.aggregator_identifier
	    }


@router.get("/pricing-plans")
def get_pricing_plans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can view pricing")

    agg_profile = current_user.aggregator_profile
    if not agg_profile:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    pricing_provider_type = PRICING_PROVIDER_TYPE_BY_PROFILE_TYPE.get(
        agg_profile.provider_type or "aggregator",
        "aggregators",
    )

    ride_types = (
        db.query(PricingRideType)
        .filter(PricingRideType.code.in_(["coordinated_transfer", "package_trip"]))
        .order_by(PricingRideType.sort_order.asc())
        .all()
    )
    ride_type_by_id = {ride.id: ride for ride in ride_types}
    vehicle_categories = (
        db.query(PricingVehicleCategory)
        .filter(
            PricingVehicleCategory.port_id == agg_profile.operating_port_id,
            PricingVehicleCategory.is_active == True,
        )
        .order_by(PricingVehicleCategory.name.asc())
        .all()
    )
    vehicle_by_id = {vehicle.id: vehicle for vehicle in vehicle_categories}
    durations = (
        db.query(PricingDuration)
        .filter(
            PricingDuration.port_id == agg_profile.operating_port_id,
            PricingDuration.is_active == True,
        )
        .order_by(PricingDuration.sort_order.asc(), PricingDuration.duration_minutes.asc())
        .all()
    )
    duration_by_id = {duration.id: duration for duration in durations}
    rules = (
        db.query(PricingRule)
        .filter(
            PricingRule.port_id == agg_profile.operating_port_id,
            PricingRule.provider_type == pricing_provider_type,
            PricingRule.is_archived == False,
        )
        .order_by(PricingRule.updated_at.desc())
        .all()
    )

    def serialize_rule(rule: PricingRule):
        ride_type = ride_type_by_id.get(rule.ride_type_id)
        vehicle = vehicle_by_id.get(rule.vehicle_category_id)
        duration = duration_by_id.get(rule.duration_id) if rule.duration_id else None
        return {
            "id": rule.id,
            "ride_type_code": ride_type.code if ride_type else None,
            "ride_type_name": ride_type.name if ride_type else None,
            "vehicle_category_id": rule.vehicle_category_id,
            "vehicle_category_name": vehicle.name if vehicle else None,
            "duration_name": duration.name if duration else None,
            "base_fare": rule.base_fare,
            "minimum_fare": rule.minimum_fare,
            "price_per_km": rule.price_per_km,
            "price_per_minute": rule.price_per_minute,
            "free_waiting_minutes": rule.free_waiting_minutes,
            "extra_waiting_charge": rule.extra_waiting_charge,
            "cancellation_fee": rule.cancellation_fee,
            "included_km": rule.included_km,
            "price_per_extra_km": rule.price_per_extra_km,
            "price_per_extra_minute": rule.price_per_extra_minute,
            "price_per_extra_stop": rule.price_per_extra_stop,
            "platform_commission_pct": rule.platform_commission_pct,
            "is_active": rule.is_active,
        }

    return {
        "provider": {
            "id": agg_profile.id,
            "name": agg_profile.company_name,
            "provider_type": agg_profile.provider_type or "aggregator",
            "pricing_provider_type": pricing_provider_type,
            "port_id": agg_profile.operating_port_id,
            "port_name": agg_profile.operating_port.name if agg_profile.operating_port else None,
        },
        "vehicle_categories": [
            {
                "id": vehicle.id,
                "code": vehicle.code,
                "name": vehicle.name,
                "seating_capacity": vehicle.seating_capacity,
                "description": vehicle.description,
            }
            for vehicle in vehicle_categories
        ],
        "durations": [
            {
                "id": duration.id,
                "ride_type_id": duration.ride_type_id,
                "name": duration.name,
                "duration_minutes": duration.duration_minutes,
            }
            for duration in durations
        ],
        "coordinated_transfer_rules": [
            serialize_rule(rule)
            for rule in rules
            if ride_type_by_id.get(rule.ride_type_id) and ride_type_by_id[rule.ride_type_id].code == "coordinated_transfer"
        ],
        "package_trip_rules": [
            serialize_rule(rule)
            for rule in rules
            if ride_type_by_id.get(rule.ride_type_id) and ride_type_by_id[rule.ride_type_id].code == "package_trip"
        ],
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
    pending_count = query.filter(CabBooking.status == BookingStatus.PENDING).count()
    active_count = query.filter(CabBooking.status.in_([BookingStatus.DRIVER_ASSIGNED, BookingStatus.CONFIRMED])).count() 
    in_progress_count = query.filter(CabBooking.status == BookingStatus.IN_PROGRESS).count()
    
    # Completed today (UTC)
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    
    completed_today = query.filter(
        CabBooking.status == BookingStatus.COMPLETED,
        CabBooking.completed_at >= today_start
    ).count()

    # Lists
    pending_bookings = query.filter(CabBooking.status == BookingStatus.PENDING).order_by(CabBooking.created_at.desc()).limit(20).all()
    active_bookings = query.filter(CabBooking.status.in_([BookingStatus.DRIVER_ASSIGNED, BookingStatus.CONFIRMED, BookingStatus.IN_PROGRESS])).order_by(CabBooking.created_at.desc()).all()
    completed_bookings = query.filter(CabBooking.status == BookingStatus.COMPLETED).order_by(CabBooking.updated_at.desc()).limit(50).all()

    def transform(b: CabBooking):
        return BookingDashboardOut(
            id=b.id,
            booking_id=b.booking_id,
            crew=CrewShortOut(
                name=b.crew.full_name,
                hp_id=b.crew.hpid or "",
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
        active_trips=[transform(b) for b in active_bookings],
        completed_requests=[transform(b) for b in completed_bookings]
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
