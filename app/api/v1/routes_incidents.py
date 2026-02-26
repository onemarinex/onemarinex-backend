from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid
from app.db.session import get_db
from app.db.models.incident import Incident, IncidentNote, IncidentStatus, IncidentType
from app.db.models.aggregator_profile import AggregatorProfile
from app.api.v1.routes_auth import get_current_user
from pydantic import BaseModel

router = APIRouter()

class IncidentNoteBase(BaseModel):
    note: str
    author_name: Optional[str] = None

class IncidentNoteResponse(IncidentNoteBase):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True

class IncidentBase(BaseModel):
    type: IncidentType
    title: str
    description: str
    reporter_name: Optional[str] = None
    reporter_role: Optional[str] = None
    reporter_id: Optional[str] = None
    trip_id: Optional[str] = None

class IncidentCreate(IncidentBase):
    pass

class IncidentResponse(IncidentBase):
    id: int
    incident_id: str
    status: IncidentStatus
    created_at: datetime
    updated_at: datetime
    notes: List[IncidentNoteResponse] = []

    class Config:
        from_attributes = True

@router.get("/", response_model=List[IncidentResponse])
async def get_incidents(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    return db.query(Incident).filter(Incident.aggregator_id == aggregator.id).all()

@router.post("/", response_model=IncidentResponse)
async def create_incident(
    incident_in: IncidentCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    incident = Incident(
        **incident_in.model_dump(),
        aggregator_id=aggregator.id,
        incident_id=f"INC-{uuid.uuid4().hex[:6].upper()}"
    )
    
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident

@router.get("/{id}", response_model=IncidentResponse)
async def get_incident(
    id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    incident = db.query(Incident).filter(Incident.id == id, Incident.aggregator_id == aggregator.id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    
    return incident

class StatusUpdate(BaseModel):
    status: IncidentStatus

@router.put("/{id}/status", response_model=IncidentResponse)
async def update_incident_status(
    id: int,
    status_update: StatusUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    incident = db.query(Incident).filter(Incident.id == id, Incident.aggregator_id == aggregator.id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    
    incident.status = status_update.status
    db.commit()
    db.refresh(incident)
    return incident

@router.post("/{id}/notes", response_model=IncidentNoteResponse)
async def add_incident_note(
    id: int,
    note_in: IncidentNoteBase,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
    if not aggregator:
        raise HTTPException(status_code=403, detail="Not an aggregator")
    
    incident = db.query(Incident).filter(Incident.id == id, Incident.aggregator_id == aggregator.id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    
    note = IncidentNote(
        incident_id=incident.id,
        note=note_in.note,
        author_name=note_in.author_name or current_user.name
    )
    
    db.add(note)
    db.commit()
    db.refresh(note)
    return note
