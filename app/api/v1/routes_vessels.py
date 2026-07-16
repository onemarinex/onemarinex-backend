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
    passport_number: Optional[str] = None
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

import csv
import io

@router.post("/{vessel_id}/crew/upload")
def upload_crew_manifest(vessel_id: int, file: UploadFile = File(...), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    filename = file.filename.lower()
    if not filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV manifest files are supported at this time.")
        
    try:
        contents = file.file.read()
        decoded = contents.decode('utf-8')
    except UnicodeDecodeError:
        try:
            decoded = contents.decode('latin-1')
        except Exception:
            raise HTTPException(status_code=400, detail="Could not decode CSV file. Please ensure it is saved with UTF-8 encoding.")
            
    csv_file = io.StringIO(decoded)
    reader = csv.reader(csv_file)
    
    try:
        headers = next(reader)
    except StopIteration:
        raise HTTPException(status_code=400, detail="CSV file is empty")
        
    headers = [h.strip().lower() for h in headers]
    
    def find_col(possible_names):
        for name in possible_names:
            if name in headers:
                return headers.index(name)
        return -1
        
    name_idx = find_col(["name", "full name", "crew name", "member name"])
    passport_idx = find_col(["passport number", "passport", "passport no", "passport_number", "passportno"])
    rank_idx = find_col(["rank", "designation", "role"])
    nat_idx = find_col(["nationality", "country", "nat"])
    eligible_idx = find_col(["shore pass allowed or not?", "shore pass allowed", "eligible", "shore_pass_eligible", "allowed", "shore pass allowed or not", "shore pass allowed or not ?"])
    valid_idx = find_col(["shore pass valid upto", "shore_pass_valid_upto", "valid upto", "validity", "expires", "valid until"])
    
    if name_idx == -1 or passport_idx == -1 or rank_idx == -1:
        raise HTTPException(
            status_code=400, 
            detail=f"Required columns (Name, Passport number, Rank) not found in CSV. Found columns: {', '.join(headers)}"
        )
        
    agent_profile = current_user.agent_profile
    port = agent_profile.assigned_port if agent_profile else None
    
    added_count = 0
    for row in reader:
        if not row or len(row) <= max(name_idx, passport_idx, rank_idx):
            continue
            
        name = row[name_idx].strip()
        passport_number = row[passport_idx].strip().upper()
        rank = row[rank_idx].strip()
        
        if not name or not passport_number or not rank:
            continue
            
        nationality = row[nat_idx].strip() if nat_idx != -1 and len(row) > nat_idx else None
        
        eligible_val = row[eligible_idx].strip().lower() if eligible_idx != -1 and len(row) > eligible_idx else "false"
        shore_pass_eligible = eligible_val in ["true", "1", "yes", "y", "checked"]
        
        shore_pass_valid_upto = None
        if valid_idx != -1 and len(row) > valid_idx:
            date_str = row[valid_idx].strip()
            if date_str:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
                    try:
                        shore_pass_valid_upto = datetime.strptime(date_str, fmt)
                        break
                    except ValueError:
                        continue
                        
        generated_hpid = generate_hpid(passport_number, nationality, port)
        
        crew = db.query(VesselCrew).filter(
            VesselCrew.vessel_id == vessel.id,
            (VesselCrew.passport_number == passport_number) | (VesselCrew.hp_id == generated_hpid)
        ).first()
        
        if not crew:
            crew = VesselCrew(
                vessel_id=vessel.id,
                name=name,
                rank=rank,
                nationality=nationality,
                hp_id=generated_hpid,
                passport_number=passport_number,
                status="Pending",
                shore_pass_eligible=shore_pass_eligible,
                shore_pass_valid_upto=shore_pass_valid_upto
            )
            db.add(crew)
        else:
            crew.name = name
            crew.rank = rank
            crew.nationality = nationality
            crew.hp_id = generated_hpid
            crew.passport_number = passport_number
            crew.shore_pass_eligible = shore_pass_eligible
            if shore_pass_valid_upto:
                crew.shore_pass_valid_upto = shore_pass_valid_upto
                
        profile = db.query(CrewProfile).filter(CrewProfile.hpid == generated_hpid).first()
        if profile:
            crew.status = "Mapped"
            existing_pass = db.query(ShorePass).filter(
                ShorePass.crew_profile_id == profile.id,
                ShorePass.port_name == port,
                ShorePass.vessel_name == vessel.name
            ).first()
            if not existing_pass:
                port_code = (port or "GEN").replace("port_", "")[:3].upper()
                vessel_code = vessel.name.replace(" ", "")[:3].upper()
                random_suffix = uuid.uuid4().hex[:4].upper()
                shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"
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
        added_count += 1
        
    db.commit()
    return {"message": f"Successfully parsed and loaded {added_count} crew members.", "filename": file.filename}

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
    crew_count: Optional[int] = 0
    total_crew: Optional[int] = 0
    eligible_crew_count: Optional[int] = 0
    crew_ashore_count: Optional[int] = 0
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: str
    
    class Config:
        from_attributes = True

class VesselPublicOut(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True

# --- Routes ---

@router.post("/", response_model=VesselOut, status_code=status.HTTP_201_CREATED)
def create_vessel(body: VesselIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can create vessels")
    
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

@router.get("/public", response_model=List[VesselPublicOut])
def get_public_vessels(
    port_code: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.db.models.agent_profile import AgentProfile
    from sqlalchemy import func, or_

    query = db.query(Vessel)
    if port_code:
        # Normalize: strip "port_" prefix and compare case-insensitively
        # to handle formats like "port_visakhapatnam", "Visakhapatnam", etc.
        port_name = port_code.replace("port_", "").replace("_", " ").lower()
        query = (
            query.join(AgentProfile, Vessel.agent_id == AgentProfile.user_id)
            .filter(
                or_(
                    func.lower(AgentProfile.assigned_port) == port_code.lower(),
                    func.lower(func.replace(AgentProfile.assigned_port, "port_", "")) == port_name,
                    func.lower(AgentProfile.assigned_port) == port_name,
                )
            )
        )
    return query.all()

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
        passport_number=body.passport_number,
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

class CrewMemberUpdate(BaseModel):
    name: Optional[str] = None
    rank: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    shore_pass_eligible: Optional[bool] = None
    shore_pass_valid_upto: Optional[datetime] = None

@router.patch("/{vessel_id}/crew/{crew_id}", response_model=CrewMemberOut)
def update_crew_member(
    vessel_id: int,
    crew_id: int,
    body: CrewMemberUpdate,
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
        raise HTTPException(status_code=403, detail="Only agents or superadmins can update crew members")

    crew = db.query(VesselCrew).filter(VesselCrew.id == crew_id, VesselCrew.vessel_id == vessel.id).first()
    if not crew:
        raise HTTPException(status_code=404, detail="Crew member not found on this vessel")
    
    if body.name is not None:
        crew.name = body.name
    if body.rank is not None:
        crew.rank = body.rank
    if body.nationality is not None:
        crew.nationality = body.nationality
    if body.passport_number is not None:
        crew.passport_number = body.passport_number.strip().upper()
    if body.shore_pass_eligible is not None:
        crew.shore_pass_eligible = body.shore_pass_eligible
    if body.shore_pass_valid_upto is not None:
        crew.shore_pass_valid_upto = body.shore_pass_valid_upto
        
    db.commit()
    db.refresh(crew)
    return crew

