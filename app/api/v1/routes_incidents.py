from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid
from app.db.session import get_db
from app.db.models.incident import Incident, IncidentNote, IncidentStatus, IncidentType
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
    port_name: Optional[str] = None

class IncidentCreate(IncidentBase):
    aggregator_id: Optional[int] = None

class IncidentResponse(IncidentBase):
    id: int
    incident_id: str
    status: IncidentStatus
    aggregator_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    notes: List[IncidentNoteResponse] = []

    class Config:
        from_attributes = True

@router.get("/monitoring")
async def get_incident_monitoring(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    base_query = db.query(Incident)
    
    if current_user.role == "aggregator":
        from app.db.models.aggregator_profile import AggregatorProfile
        aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
        if not aggregator:
            raise HTTPException(status_code=403, detail="Not an aggregator")
        base_query = base_query.filter(Incident.aggregator_id == aggregator.id)
    
    elif current_user.role == "agent":
        from app.db.models.agent_profile import AgentProfile
        agent = db.query(AgentProfile).filter(AgentProfile.user_id == current_user.id).first()
        if not agent or not agent.assigned_port:
            return {
                "active_crew": 0,
                "active_aggregator": 0,
                "active_incidents": [],
                "resolved_incidents": []
            }
        base_query = base_query.filter(Incident.port_name == agent.assigned_port)
    
    else:
        raise HTTPException(status_code=403, detail="Not authorized")

    active_incidents = base_query.filter(Incident.status.in_([IncidentStatus.ACTIVE, IncidentStatus.INVESTIGATING])).all()
    resolved_incidents = base_query.filter(Incident.status == IncidentStatus.RESOLVED).all()
    
    active_crew = sum(1 for inc in active_incidents if inc.type == IncidentType.CREW)
    active_aggregator = sum(1 for inc in active_incidents if inc.type == IncidentType.DRIVER) # Assuming DRIVER type represents aggregator incidents in this context as per original model

    return {
        "active_crew": active_crew,
        "active_aggregator": active_aggregator,
        "active_incidents": active_incidents,
        "resolved_incidents": resolved_incidents
    }

@router.get("/", response_model=List[IncidentResponse])
async def get_incidents(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role == "aggregator":
        from app.db.models.aggregator_profile import AggregatorProfile
        aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
        if not aggregator:
            raise HTTPException(status_code=403, detail="Not an aggregator")
        return db.query(Incident).filter(Incident.aggregator_id == aggregator.id).all()
    
    elif current_user.role == "agent":
        from app.db.models.agent_profile import AgentProfile
        agent = db.query(AgentProfile).filter(AgentProfile.user_id == current_user.id).first()
        if not agent or not agent.assigned_port:
            return []
        return db.query(Incident).filter(Incident.port_name == agent.assigned_port).all()
    
    else:
        raise HTTPException(status_code=403, detail="Not authorized to list incidents")

@router.post("/", response_model=IncidentResponse)
async def create_incident(
    incident_in: IncidentCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    incident_data = incident_in.model_dump()
    incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
    
    if current_user.role == "aggregator":
        from app.db.models.aggregator_profile import AggregatorProfile
        aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
        if not aggregator:
            raise HTTPException(status_code=403, detail="Not an aggregator")
        
        incident = Incident(
            **incident_data,
            aggregator_id=aggregator.id,
            incident_id=incident_id
        )
    elif current_user.role == "crew":
        from app.db.models.crew_profile import CrewProfile
        crew = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
        
        # Remove fields that we will set explicitly to avoid "multiple values for keyword argument"
        for field in ["reporter_name", "reporter_role", "reporter_id", "type"]:
            incident_data.pop(field, None)

        incident = Incident(
            **incident_data,
            incident_id=incident_id,
            reporter_name=current_user.name,
            reporter_role=crew.rank if crew else "Crew",
            reporter_id=crew.passport_number if crew else None,
            port_name=crew.current_port if crew else None,
            type=IncidentType.CREW
        )
    else:
        raise HTTPException(status_code=403, detail="Not authorized to create incidents")
    
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident

@router.get("/crew/recipients", response_model=List[dict])
async def get_incident_recipients(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can fetch recipients")
    
    from app.db.models.crew_profile import CrewProfile
    from app.db.models.aggregator_profile import AggregatorProfile
    
    crew = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not crew or not crew.current_port:
        return [{"id": 0, "name": "General Support"}]
    
    aggregators = db.query(AggregatorProfile).filter(AggregatorProfile.operating_port == crew.current_port).all()
    
    recipients = [{"id": 0, "name": "General Support"}]
    for agg in aggregators:
        recipients.append({
            "id": agg.id,
            "name": agg.company_name
        })
    
    return recipients

@router.get("/{id}", response_model=IncidentResponse)
async def get_incident(
    id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    query = db.query(Incident).filter(Incident.id == id)
    
    if current_user.role == "aggregator":
        from app.db.models.aggregator_profile import AggregatorProfile
        aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == current_user.id).first()
        if not aggregator:
             raise HTTPException(status_code=403, detail="Not authorized")
        incident = query.filter(Incident.aggregator_id == aggregator.id).first()
    elif current_user.role == "agent":
        from app.db.models.agent_profile import AgentProfile
        agent = db.query(AgentProfile).filter(AgentProfile.user_id == current_user.id).first()
        if not agent or not agent.assigned_port:
             raise HTTPException(status_code=403, detail="Not authorized")
        incident = query.filter(Incident.port_name == agent.assigned_port).first()
    else:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    
    return incident

class StatusUpdate(BaseModel):
    status: IncidentStatus

@router.patch("/{id}/status", response_model=IncidentResponse)
async def update_incident_status(
    id: int,
    status_update: StatusUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    incident = await get_incident(id, db, current_user)
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
    incident = await get_incident(id, db, current_user)
    
    note = IncidentNote(
        incident_id=incident.id,
        note=note_in.note,
        author_name=note_in.author_name or current_user.name
    )
    
    db.add(note)
    db.commit()
    db.refresh(note)
    return note
