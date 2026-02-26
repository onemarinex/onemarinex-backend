from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.models.crew_profile import CrewProfile
from app.db.models.cab_booking import CabBooking
from app.db.models.shore_pass import ShorePass
from app.api.v1.routes_users import get_current_user
from app.db.models.user import User

router = APIRouter()

# --- Pydantic Schemas ---

class CrewMemberIn(BaseModel):
    name: str
    rank: str
    nationality: Optional[str] = None
    hp_id: Optional[str] = None
    expiry_date: Optional[datetime] = None
    status: Optional[str] = "Pending"

class CrewMemberOut(BaseModel):
    id: int
    name: str
    rank: str
    nationality: Optional[str] = None
    hp_id: Optional[str] = None
    expiry_date: Optional[datetime] = None
    status: str
    
    class Config:
        from_attributes = True

class CabBookingOut(BaseModel):
    id: int
    pickup_address: str
    drop_address: str
    status: str
    
    class Config:
        from_attributes = True

class CrewProfileOut(BaseModel):
    id: int
    name: str
    rank: str
    hp_id: Optional[str] = None
    status: str
    visits: List[str] = []
    bookings: List[CabBookingOut] = []

    class Config:
        from_attributes = True

class VesselIn(BaseModel):
    name: str
    imo_number: str
    vessel_type: str
    berth_assignment: Optional[str] = None
    crew_count: Optional[int] = 0
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: Optional[str] = "Active"

@router.post("/{vessel_id}/crew/upload")
def upload_crew_manifest(vessel_id: int, file: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    return {"message": f"Successfully received manifest for {vessel.name}", "filename": file.filename}

@router.get("/crew/{hp_id}/profile", response_model=CrewProfileOut)
def get_crew_profile(hp_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    v_crew = db.query(VesselCrew).filter(VesselCrew.hp_id == hp_id).first()
    if not v_crew:
        raise HTTPException(status_code=404, detail="Crew member not found")
    
    c_profile = db.query(CrewProfile).filter(CrewProfile.passport_number == hp_id).first()
    
    bookings = []
    visits = []
    if c_profile:
        bookings = db.query(CabBooking).filter(CabBooking.crew_id == c_profile.id).all()
        # Filter shoreline history
        shore_passes = db.query(ShorePass).filter(ShorePass.crew_profile_id == c_profile.id).all()
        visits = [sp.port_name for sp in shore_passes if sp.port_name]

    return {
        "id": v_crew.id,
        "name": v_crew.name,
        "rank": v_crew.rank,
        "hp_id": v_crew.hp_id,
        "status": v_crew.status,
        "visits": visits,
        "bookings": bookings
    }

class VesselOut(BaseModel):
    id: int
    name: str
    imo_number: str
    vessel_type: str
    berth_assignment: Optional[str] = None
    crew_count: int
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: str
    
    class Config:
        from_attributes = True

# --- Routes ---

@router.post("/", response_model=VesselOut, status_code=status.HTTP_201_CREATED)
def create_vessel(body: VesselIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can create vessels")
    
    vessel = Vessel(
        agent_id=current_user.id,
        name=body.name,
        imo_number=body.imo_number,
        vessel_type=body.vessel_type,
        berth_assignment=body.berth_assignment,
        crew_count=body.crew_count,
        eta=body.eta,
        etd=body.etd,
        status=body.status
    )
    db.add(vessel)
    try:
        db.commit()
        db.refresh(vessel)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Vessel IMO possibly already exists")
    
    return vessel

@router.get("/", response_model=List[VesselOut])
def get_vessels(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access their vessels")
    
    return db.query(Vessel).filter(Vessel.agent_id == current_user.id).all()

@router.get("/{vessel_id}", response_model=VesselOut)
def get_vessel_details(vessel_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return vessel

@router.get("/{vessel_id}/crew", response_model=List[CrewMemberOut])
def get_crew_manifest(vessel_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    return vessel.crew_manifest

@router.post("/{vessel_id}/crew", response_model=CrewMemberOut, status_code=status.HTTP_201_CREATED)
def add_crew_member(vessel_id: int, body: CrewMemberIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    crew = VesselCrew(
        vessel_id=vessel.id,
        name=body.name,
        rank=body.rank,
        nationality=body.nationality,
        hp_id=body.hp_id,
        expiry_date=body.expiry_date,
        status=body.status
    )
    db.add(crew)
    db.commit()
    db.refresh(crew)
    return crew
