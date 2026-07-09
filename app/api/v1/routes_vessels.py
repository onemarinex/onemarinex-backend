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
from app.db.models.user import User
from app.api.v1.routes_auth import get_current_user
from app.services.crew_service import generate_hpid
import uuid

router = APIRouter()

# --- Pydantic Schemas ---

class CrewMemberIn(BaseModel):
    name: str
    rank: str
    nationality: Optional[str] = None
    passport_number: str
    status: Optional[str] = "Pending"
    shore_pass_eligible: Optional[bool] = False
    shore_pass_valid_upto: Optional[datetime] = None

class CrewMemberOut(BaseModel):
    id: int
    name: str
    rank: str
    nationality: Optional[str] = None
    hp_id: Optional[str] = None
    expiry_date: Optional[datetime] = None
    status: str
    shore_pass_eligible: bool
    shore_pass_valid_upto: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class EligibilityUpdateIn(BaseModel):
    shore_pass_eligible: bool

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
    flag: Optional[str] = None
    crew_count: Optional[int] = 0
    total_crew: Optional[int] = 0
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
    
    c_profile = db.query(CrewProfile).filter(CrewProfile.hpid == hp_id).first()
    
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
    flag: Optional[str] = None
    crew_count: int
    total_crew: Optional[int] = 0
    eligible_crew_count: Optional[int] = 0
    crew_ashore_count: Optional[int] = 0
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: str
    
    class Config:
        from_attributes = True

# --- Routes ---

@router.post("/", response_model=VesselOut, status_code=status.HTTP_201_CREATED)
def create_vessel(body: VesselIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["agent", "superadmin"]:
        raise HTTPException(status_code=403, detail="Only agents or superadmins can create vessels")
    
    c_count = body.crew_count if body.crew_count is not None else 0
    if body.total_crew is not None:
        c_count = body.total_crew

    vessel = Vessel(
        agent_id=current_user.id if current_user.role == "agent" else 1,
        name=body.name,
        imo_number=body.imo_number,
        vessel_type=body.vessel_type,
        berth_assignment=body.berth_assignment,
        flag=body.flag,
        crew_count=c_count,
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

@router.patch("/{vessel_id}", response_model=VesselOut)
def update_vessel(vessel_id: int, body: VesselIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    if current_user.role == "agent" and vessel.agent_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this vessel")
        
    vessel.name = body.name
    vessel.imo_number = body.imo_number
    vessel.vessel_type = body.vessel_type
    vessel.berth_assignment = body.berth_assignment
    vessel.flag = body.flag
    
    c_count = body.crew_count if body.crew_count is not None else 0
    if body.total_crew is not None:
        c_count = body.total_crew
    vessel.crew_count = c_count
    
    vessel.eta = body.eta
    vessel.etd = body.etd
    if body.status:
        vessel.status = body.status
        
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
    
    agent_profile = current_user.agent_profile
    port = agent_profile.assigned_port if agent_profile else None
    
    # Generate HPID based on Passport, Nationality, and Port
    generated_hpid = generate_hpid(body.passport_number, body.nationality, port)
    
    crew = VesselCrew(
        vessel_id=vessel.id,
        name=body.name,
        rank=body.rank,
        nationality=body.nationality,
        hp_id=generated_hpid,
        status=body.status,
        shore_pass_eligible=body.shore_pass_eligible if body.shore_pass_eligible is not None else False,
        shore_pass_valid_upto=body.shore_pass_valid_upto
    )
    db.add(crew)
    
    # Check if a matching CrewProfile exists to automatically generate a ShorePass
    profile = db.query(CrewProfile).filter(CrewProfile.hpid == generated_hpid).first()
    if profile:
        # Create ShorePass automatically
        port_code = (port or "GEN").replace("port_", "")[:3].upper()
        vessel_code = vessel.name.replace(" ", "")[:3].upper()
        random_suffix = uuid.uuid4().hex[:4].upper()
        shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"
        
        # Derive agent name
        port_display = (port or "General").replace("port_", "").replace("_", " ").title()
        agent_name = f"{port_display} Port Authority"
        
        new_pass = ShorePass(
            crew_profile_id=profile.id,
            agent_name=agent_name,
            shore_pass_id=shore_pass_id,
            port_name=port,
            vessel_name=vessel.name,
            is_verified=False,
            status="pending"
        )
        db.add(new_pass)
        print(f"DEBUG: Automated ShorePass created for {body.name} (HPID: {generated_hpid})")

    db.commit()
    db.refresh(crew)
    return crew

@router.get("/public", response_model=List[VesselOut])
def get_public_vessels(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(Vessel).all()

@router.patch("/{vessel_id}/crew/{crew_id}/eligibility", response_model=CrewMemberOut)
def update_crew_eligibility(
    vessel_id: int, 
    crew_id: int, 
    body: EligibilityUpdateIn, 
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    if current_user.role == "agent":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found or unauthorized")
    elif current_user.role == "superadmin":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found")
    else:
        raise HTTPException(status_code=403, detail="Only agents or superadmins can toggle eligibility")

    crew = db.query(VesselCrew).filter(VesselCrew.id == crew_id, VesselCrew.vessel_id == vessel.id).first()
    if not crew:
        raise HTTPException(status_code=404, detail="Crew member not found on this vessel")
    
    crew.shore_pass_eligible = body.shore_pass_eligible
    db.commit()
    db.refresh(crew)
    return crew
