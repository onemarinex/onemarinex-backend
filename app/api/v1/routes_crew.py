from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date, datetime, timedelta
import uuid

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.cab_booking import CabBooking
from app.db.models.cab_pricing import CabPricing
from app.api.v1.routes_auth import get_current_user
from pydantic import BaseModel

router = APIRouter()

class ProfileUpdateIn(BaseModel):
    full_name: Optional[str] = None
    rank: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    current_port: Optional[str] = None
    vessel: Optional[str] = None


class CrewProfileOut(BaseModel):
    id: int
    full_name: str
    rank: str
    nationality: str
    passport_number: str
    date_of_birth: date
    current_port: Optional[str]
    vessel: Optional[str]

    class Config:
        from_attributes = True

class ShorePassOut(BaseModel):
    id: int
    agent_name: Optional[str]
    shore_pass_id: str
    port_name: Optional[str]
    vessel_name: Optional[str]
    out_time: Optional[datetime]
    in_time: Optional[datetime]
    expires_at: Optional[datetime]
    is_verified: bool
    status: str
    rejection_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CabBookingCreateIn(BaseModel):
    pickup_address: str
    pickup_lat: float
    pickup_lng: float
    drop_address: str
    drop_lat: float
    drop_lng: float
    vehicle_type: str  # 'ac', 'premium', 'xl'
    vehicle_name: str
    estimated_price: float
    distance_km: float
    num_passengers: int = 1
    port: Optional[str] = None
    crew_member_ids: Optional[List[str]] = None  # List of HeyPorts IDs
    scheduled_time: Optional[datetime] = None

class CabBookingCreateOut(BaseModel):
    booking_id: str
    otp: str
    status: str
    agent_number: str

class CabBookingDetailsOut(BaseModel):
    booking_id: str
    vehicle_name: str
    estimated_price: float
    drop_address: str
    num_passengers: int
    driver_name: Optional[str]
    driver_phone: Optional[str]
    otp: str
    agent_number: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True

class CabBookingOut(BaseModel):
    id: int
    booking_id: str
    pickup_address: str
    drop_address: str
    vehicle_type: str
    vehicle_name: str
    estimated_price: float
    num_passengers: int
    status: str
    scheduled_time: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

class CabEstimate(BaseModel):
    vehicle_type: str
    name: str
    estimated_price: float
    distance_km: float
    base_fare: float
    per_km_rate: float

@router.patch("/profile", response_model=dict)
def update_crew_profile(
    body: ProfileUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(
            status_code=403, 
            detail=f"Only crew can update crew profile. Your role: '{current_user.role}'"
        )
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    # Partial update: only update if field is present in request
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    return {"message": "Profile updated successfully"}

@router.get("/profile", response_model=CrewProfileOut)
def get_crew_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    return profile

class GenerateShorePassIn(BaseModel):
    port_name: Optional[str] = None
    vessel_name: Optional[str] = None

@router.post("/generate-shorepass", response_model=ShorePassOut)
def generate_shorepass(
    body: GenerateShorePassIn = GenerateShorePassIn(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(
            status_code=403, 
            detail=f"Only crew can generate shore pass. Your role: '{current_user.role}'"
        )
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    # Use explicitly passed port/vessel, fallback to profile values
    port = body.port_name or profile.current_port
    vessel = body.vessel_name or profile.vessel

    if not port or not vessel:
        raise HTTPException(status_code=400, detail="Port and Vessel must be selected first")

    # Derive agent name from port (e.g. "port_singapore" -> "Singapore Port Authority")
    port_display = port.replace("port_", "").replace("_", " ").title()
    agent_name = f"{port_display} Port Authority"

    # Build unique shore pass ID: port code + vessel code + random
    port_code = port.replace("port_", "")[:3].upper()          # e.g. "SIN"
    vessel_code = vessel.replace("vessel_", "V")[:3].upper()   # e.g. "V1"
    random_suffix = uuid.uuid4().hex[:4].upper()
    shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"

    # Generate shore pass
    new_pass = ShorePass(
        crew_profile_id=profile.id,
        agent_name=agent_name,
        shore_pass_id=shore_pass_id,
        port_name=port,
        vessel_name=vessel,
        out_time=datetime.utcnow(),
        in_time=datetime.utcnow() + timedelta(days=7),
        expires_at=datetime.utcnow() + timedelta(days=7),
        is_verified=False,
        status="pending"
    )
    
    db.add(new_pass)
    try:
        db.commit()
        db.refresh(new_pass)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    return new_pass

@router.get("/shorepass", response_model=Optional[ShorePassOut])
def get_current_shorepass(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        return None
    
    # Get the latest shore pass
    last_pass = db.query(ShorePass).filter(ShorePass.crew_profile_id == profile.id).order_by(ShorePass.created_at.desc()).first()
    return last_pass

@router.get("/shorepass/history", response_model=List[ShorePassOut])
def get_shorepass_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all shore passes for the current user (newest first)"""
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        return []
    
    passes = db.query(ShorePass).filter(
        ShorePass.crew_profile_id == profile.id
    ).order_by(ShorePass.created_at.desc()).all()
    return passes

@router.post("/cab/book", response_model=CabBookingCreateOut)
def book_cab(
    body: CabBookingCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    # Generate booking ID: CAB-XXXXXXXX
    booking_id = f"CAB-{uuid.uuid4().hex[:8].upper()}"
    
    print(f"DEBUG: Receiving booking with price: {body.estimated_price}")
    
    # Use the crew member's fixed lifetime OTP
    otp = profile.ride_otp or "1234"
    
    from app.db.models.cab_booking import VehicleType, BookingStatus
    new_booking = CabBooking(
        booking_id=booking_id,
        crew_id=profile.id,
        pickup_address=body.pickup_address,
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        drop_address=body.drop_address,
        drop_lat=body.drop_lat,
        drop_lng=body.drop_lng,
        vehicle_type=VehicleType(body.vehicle_type),
        vehicle_name=body.vehicle_name,
        estimated_price=body.estimated_price,
        distance_km=body.distance_km,
        num_passengers=body.num_passengers,
        port=body.port or profile.current_port,
        crew_member_ids=body.crew_member_ids,
        scheduled_time=body.scheduled_time,
        otp=otp,
        driver_name=None,
        driver_phone=None,
        agent_number="+91 9876543251",
        status=BookingStatus.PENDING
    )
    
    db.add(new_booking)
    try:
        db.commit()
        db.refresh(new_booking)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    return CabBookingCreateOut(
        booking_id=new_booking.booking_id,
        otp=new_booking.otp,
        status=new_booking.status.value,
        agent_number=new_booking.agent_number
    )

# Duplicate route removed/commented out
# @router.get("/cab/history", response_model=List[CabBookingOut])
# def get_cab_history(
#     db: Session = Depends(get_db),
#     current_user: User = Depends(get_current_user)
# ):
#     profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
#     if not profile:
#         return []
#     
#     history = db.query(CabBooking).filter(CabBooking.crew_id == profile.id).order_by(CabBooking.created_at.desc()).all()
#     return history

@router.get("/cab/estimate", response_model=List[CabEstimate])
def get_cab_estimates(
    pickup_lat: float,
    pickup_lng: float,
    drop_lat: float,
    drop_lng: float,
    db: Session = Depends(get_db)
):
    # Mock distance calculation (Euclidean * 111 for rough km)
    # Ideally replace this with a call to Ola Maps Directions API
    distance = ((pickup_lat - drop_lat)**2 + (pickup_lng - drop_lng)**2)**0.5 * 111
    
    pricings = db.query(CabPricing).all()
    
    # If table is empty, return default pricing
    if not pricings:
        # Seed values for initial run if empty
        default_pricings = [
            {"type": "ac", "name": "Cab AC", "base": 50, "rate": 15, "min": 100},
            {"type": "premium", "name": "Cab Premium AC", "base": 80, "rate": 22, "min": 180},
            {"type": "xl", "name": "Cab XL AC", "base": 120, "rate": 30, "min": 250},
        ]
        res = []
        for dp in default_pricings:
            est_price = dp["base"] + (distance * dp["rate"])
            final_price = max(est_price, dp["min"])
            res.append(CabEstimate(
                vehicle_type=dp["type"],
                name=dp["name"],
                estimated_price=round(final_price, 2),
                distance_km=round(distance, 2),
                base_fare=float(dp["base"]),
                per_km_rate=float(dp["rate"])
            ))
        return res

    estimates = []
    for p in pricings:
        est_price = p.base_fare + (distance * p.per_km_rate)
        final_price = max(est_price, p.minimum_fare)
        estimates.append(CabEstimate(
            vehicle_type=p.vehicle_type,
            name=p.vehicle_type, # Or add a 'name' field to model if needed
            estimated_price=round(final_price, 2),
            distance_km=round(distance, 2),
            base_fare=p.base_fare,
            per_km_rate=p.per_km_rate
        ))
    return estimates

    return {"message": "Booking cancelled successfully", "booking_id": booking_id}

@router.get("/cab/bookings/{booking_id}", response_model=CabBookingDetailsOut)
def get_booking_details(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get detailed information about a specific booking"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view bookings")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    booking = db.query(CabBooking).filter(
        CabBooking.booking_id == booking_id,
        CabBooking.crew_id == profile.id
    ).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    return CabBookingDetailsOut(
        booking_id=booking.booking_id,
        vehicle_name=booking.vehicle_name,
        estimated_price=float(booking.estimated_price),
        drop_address=booking.drop_address,
        num_passengers=booking.num_passengers,
        driver_name=booking.driver_name or "Not Yet Assigned",
        driver_phone=booking.driver_phone or "Not Yet Assigned",
        otp=booking.otp,
        agent_number=booking.agent_number,
        status=booking.status.value,
        created_at=booking.created_at
    )

@router.put("/cab/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cancel a cab booking"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can cancel bookings")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    booking = db.query(CabBooking).filter(
        CabBooking.booking_id == booking_id,
        CabBooking.crew_id == profile.id
    ).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    from app.db.models.cab_booking import BookingStatus
    if booking.status == BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Booking already cancelled")
    
    if booking.status == BookingStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Cannot cancel completed booking")
    
    booking.status = BookingStatus.CANCELLED
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to cancel booking: {str(e)}")
    
    return {"message": "Booking cancelled successfully", "booking_id": booking_id}

@router.get("/cab/history", response_model=List[CabBookingOut])
def get_booking_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all cab bookings for the current user"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view booking history")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    bookings = db.query(CabBooking).filter(
        CabBooking.crew_id == profile.id
    ).order_by(CabBooking.created_at.desc()).all()
    
    return [
        CabBookingOut(
            id=booking.id,
            booking_id=booking.booking_id,
            pickup_address=booking.pickup_address,
            drop_address=booking.drop_address,
            vehicle_type=booking.vehicle_type.value,
            vehicle_name=booking.vehicle_name,
            estimated_price=float(booking.estimated_price),
            num_passengers=booking.num_passengers,
            status=booking.status.value,
            scheduled_time=booking.scheduled_time,
            created_at=booking.created_at
        )
        for booking in bookings
    ]
