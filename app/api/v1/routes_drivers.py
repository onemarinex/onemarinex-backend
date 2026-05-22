from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid
from app.db.session import get_db
from app.db.models.driver import Driver
from app.db.models.aggregator_profile import AggregatorProfile
from app.api.v1.routes_auth import get_current_user
from app.api.v1.deps import get_current_driver
from app.services.auth import decode_subject, get_password_hash, verify_password
from pydantic import BaseModel
import uuid

router = APIRouter()

class DriverBase(BaseModel):
    name: str
    email: Optional[str] = None
    phone: str
    license_number: Optional[str] = None
    vehicle_number: str
    vehicle_type: Optional[str] = None
    vehicle_name: Optional[str] = None
    profile_image: Optional[str] = None

class DriverCreate(DriverBase):
    password: str

class DriverResponse(DriverBase):
    id: int
    hpid: Optional[str] = None
    status: str
    rating: float
    total_rides: int = 0
    today_rides: int = 0
    is_reset_requested: int = 0

    class Config:
        from_attributes = True

@router.get("/", response_model=List[DriverResponse])
async def get_drivers(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    return db.query(Driver).filter(Driver.aggregator_id == aggregator.id).all()

@router.post("/", response_model=DriverResponse)
async def create_driver(
    driver_in: DriverCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    from app.services.auth import get_password_hash
    
    driver_data = driver_in.model_dump()
    password = driver_data.pop('password')
    
    driver = Driver(
        **driver_data,
        hashed_password=get_password_hash(password),
        aggregator_id=aggregator.id,
        is_temp_password=1
    )
    
    # Generate HPID if not provided
    if not driver.hpid:
        driver.hpid = f"HPID-D-{uuid.uuid4().hex[:6].upper()}"
        
    db.add(driver)
    db.commit()
    db.refresh(driver)
    return driver

class DriverLoginIn(BaseModel):
    email: str
    password: str

@router.post("/login")
async def driver_login(body: DriverLoginIn, db: Session = Depends(get_db)):
    from app.services.auth import verify_password, create_access_token, create_refresh_token
    from datetime import timedelta
    from app.core.config import settings

    driver = db.query(Driver).filter(Driver.email == body.email).first()
    if not driver or not verify_password(body.password, driver.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(
        subject=driver.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=driver.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
    return {
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "role": "driver",
        "must_change_password": driver.is_temp_password == 1,
        "name": driver.name,
        "aggregator_name": driver.aggregator.company_name if driver.aggregator else "sri venkateswara Agent"
    }

class DriverRefreshIn(BaseModel):
    refresh_token: str

@router.post("/refresh")
async def driver_refresh(body: DriverRefreshIn, db: Session = Depends(get_db)):
    from app.services.auth import verify_refresh_token, create_access_token, create_refresh_token
    from datetime import timedelta
    from app.core.config import settings

    email = verify_refresh_token(body.refresh_token)
    if not email:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    driver = db.query(Driver).filter(Driver.email == email).first()
    if not driver:
        raise HTTPException(status_code=401, detail="Driver not found")

    access_token = create_access_token(
        subject=driver.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=driver.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "role": "driver",
    }

class UpdatePasswordIn(BaseModel):
    old_password: str
    new_password: str

@router.post("/update-password")
async def update_password(
    body: UpdatePasswordIn,
    db: Session = Depends(get_db),
    current_driver: Driver = Depends(get_current_driver)
):
    if not verify_password(body.old_password, current_driver.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect old password")
        
    current_driver.hashed_password = get_password_hash(body.new_password)
    current_driver.is_temp_password = 0
    db.commit()
    return {"message": "Password updated successfully"}

class ProfileUpdateIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    old_password: Optional[str] = None
    new_password: Optional[str] = None

@router.get("/profile", response_model=DriverResponse)
async def get_driver_profile(current_driver: Driver = Depends(get_current_driver)):
    return current_driver

@router.patch("/profile", response_model=DriverResponse)
async def update_driver_profile(
    body: ProfileUpdateIn,
    db: Session = Depends(get_db),
    current_driver: Driver = Depends(get_current_driver)
):
    if body.name:
        current_driver.name = body.name
    if body.email:
        # Check if email is already taken by another driver
        existing = db.query(Driver).filter(Driver.email == body.email, Driver.id != current_driver.id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already taken")
        current_driver.email = body.email
    if body.phone:
        current_driver.phone = body.phone
        
    if body.new_password:
        if not body.old_password:
            raise HTTPException(status_code=400, detail="Old password required to set new password")
        if not verify_password(body.old_password, current_driver.hashed_password):
            raise HTTPException(status_code=400, detail="Incorrect old password")
        current_driver.hashed_password = get_password_hash(body.new_password)
        current_driver.is_temp_password = 0

    db.commit()
    db.refresh(current_driver)
    return current_driver

@router.get("/assigned-rides")
async def get_assigned_rides(
    db: Session = Depends(get_db),
    # For now, we'll use a hack or implement a proper get_current_driver
    authorization: Optional[str] = Header(None)
):
    from app.services.auth import decode_subject
    from app.db.models.cab_booking import CabBooking
    
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = authorization.split(" ")[1]
    email = decode_subject(token)
    
    driver = db.query(Driver).filter(Driver.email == email).first()
    if not driver:
        raise HTTPException(status_code=401, detail="Invalid driver token")
        
    from sqlalchemy.orm import joinedload
    rides = db.query(CabBooking).options(joinedload(CabBooking.crew)).filter(
        CabBooking.driver_id == driver.id,
        CabBooking.status.in_(['driver_assigned', 'confirmed', 'in_progress'])
    ).all()
    
    return rides

@router.get("/rides")
async def get_driver_rides(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None)
):
    from app.services.auth import decode_subject
    from app.db.models.cab_booking import CabBooking
    from sqlalchemy.orm import joinedload
    
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = authorization.split(" ")[1]
    email = decode_subject(token)
    
    driver = db.query(Driver).filter(Driver.email == email).first()
    if not driver:
        raise HTTPException(status_code=401, detail="Invalid driver token")
        
    rides = db.query(CabBooking).options(joinedload(CabBooking.crew)).filter(
        CabBooking.driver_id == driver.id
    ).order_by(CabBooking.created_at.desc()).all()
    
    return rides

@router.post("/rides/{ride_id}/accept")
async def accept_ride(
    ride_id: int,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None)
):
    from app.db.models.cab_booking import CabBooking, BookingStatus
    from app.services.auth import decode_subject
    
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = authorization.split(" ")[1]
    email = decode_subject(token)
    driver = db.query(Driver).filter(Driver.email == email).first()
    if not driver:
        raise HTTPException(status_code=401, detail="Invalid driver")

    ride = db.query(CabBooking).filter(CabBooking.id == ride_id, CabBooking.driver_id == driver.id).first()
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found or not assigned to you")
    
    ride.status = BookingStatus.CONFIRMED
    db.commit()
    db.refresh(ride)
    return {"message": "Ride accepted", "otp": ride.crew.ride_otp if ride.crew else "1234"}

@router.post("/rides/{ride_id}/arrive")
async def driver_arrived(
    ride_id: int,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None)
):
    from app.db.models.cab_booking import CabBooking
    from app.services.auth import decode_subject
    
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = authorization.split(" ")[1]
    email = decode_subject(token)
    driver = db.query(Driver).filter(Driver.email == email).first()
    
    ride = db.query(CabBooking).filter(CabBooking.id == ride_id, CabBooking.driver_id == driver.id).first()
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
        
    ride.arrived_at = datetime.utcnow()
    ride.status = BookingStatus.ARRIVED
    db.commit()
    return {"message": "Arrival recorded"}

class StartRideIn(BaseModel):
    otp: str

@router.post("/rides/{ride_id}/start")
async def start_ride(
    ride_id: int,
    body: StartRideIn,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None)
):
    from app.db.models.cab_booking import CabBooking, BookingStatus
    from app.services.auth import decode_subject
    
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = authorization.split(" ")[1]
    email = decode_subject(token)
    driver = db.query(Driver).filter(Driver.email == email).first()
    
    ride = db.query(CabBooking).filter(CabBooking.id == ride_id, CabBooking.driver_id == driver.id).first()
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
        
    # Verify against crew member's fixed lifetime OTP
    if not ride.crew or ride.crew.ride_otp != body.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")
        
    ride.status = BookingStatus.IN_PROGRESS
    ride.started_at = datetime.utcnow()
    
    # Mark driver as busy
    driver.status = "Busy"
    
    db.commit()
    return {"message": "Ride started"}

@router.post("/rides/{ride_id}/complete")
async def complete_ride(
    ride_id: int,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None)
):
    from app.db.models.cab_booking import CabBooking, BookingStatus
    from app.services.auth import decode_subject
    
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = authorization.split(" ")[1]
    email = decode_subject(token)
    driver = db.query(Driver).filter(Driver.email == email).first()
    
    ride = db.query(CabBooking).filter(CabBooking.id == ride_id, CabBooking.driver_id == driver.id).first()
    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
        
    ride.status = BookingStatus.COMPLETED
    ride.completed_at = datetime.utcnow()
    
    # Mark driver as available
    driver.status = "Available"
    
    db.commit()
    return {"message": "Ride completed"}

@router.get("/{driver_id}", response_model=DriverResponse)
async def get_driver_details(
    driver_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    driver = db.query(Driver).filter(Driver.id == driver_id, Driver.aggregator_id == aggregator.id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
        
    # Mock some ride stats for now
    from sqlalchemy import func
    from app.db.models.cab_booking import CabBooking
    
    total_rides = db.query(CabBooking).filter(CabBooking.driver_id == driver.id).count()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_rides = db.query(CabBooking).filter(
        CabBooking.driver_id == driver.id,
        CabBooking.created_at >= today_start
    ).count()
    
    driver.total_rides = total_rides
    driver.today_rides = today_rides
    
    return driver

@router.delete("/{driver_id}")
async def delete_driver(
    driver_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    driver = db.query(Driver).filter(Driver.id == driver_id, Driver.aggregator_id == aggregator.id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    db.delete(driver)
    db.commit()
    return {"message": "Driver deleted"}

class ResetRequestIn(BaseModel):
    email: str

@router.post("/request-reset")
async def request_password_reset(body: ResetRequestIn, db: Session = Depends(get_db)):
    driver = db.query(Driver).filter(Driver.email == body.email).first()
    if not driver:
        # For security, we might not want to disclose if email exists, 
        # but the user asks to "keep a forgout password option near login" 
        # and "this request should go to the aggregator".
        raise HTTPException(status_code=404, detail="Driver with this email not found")
    
    driver.is_reset_requested = 1
    db.commit()
    return {"message": "Reset request sent to your aggregator"}

class ResetPasswordIn(BaseModel):
    new_password: Optional[str] = None

@router.post("/{driver_id}/reset-password")
async def reset_driver_password(
    driver_id: int,
    body: Optional[ResetPasswordIn] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    driver = db.query(Driver).filter(Driver.id == driver_id, Driver.aggregator_id == aggregator.id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    from app.services.auth import get_password_hash
    
    reset_password = body.new_password if body and body.new_password else "driver123"
    driver.hashed_password = get_password_hash(reset_password)
    driver.is_temp_password = 1
    driver.is_reset_requested = 0
    db.commit()
    
    msg = f"Password updated to '{reset_password}'" if body and body.new_password else f"Password reset to '{reset_password}'"
    return {"message": msg, "temp_password": reset_password}
