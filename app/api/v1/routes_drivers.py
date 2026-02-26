from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid
from app.db.session import get_db
from app.db.models.driver import Driver
from app.db.models.aggregator_profile import AggregatorProfile
from app.api.v1.routes_auth import get_current_user
from pydantic import BaseModel

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
    pass

class DriverResponse(DriverBase):
    id: int
    hpid: Optional[str] = None
    status: str
    rating: float
    total_rides: int = 0
    today_rides: int = 0

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
    
    driver = Driver(
        **driver_in.model_dump(),
        aggregator_id=aggregator.id
    )
    
    # Generate HPID if not provided
    if not driver.hpid:
        driver.hpid = f"HPID-D-{uuid.uuid4().hex[:6].upper()}"
        
    db.add(driver)
    db.commit()
    db.refresh(driver)
    return driver

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
