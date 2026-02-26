from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from datetime import datetime

from app.db.session import get_db
from app.db.models.agent_profile import AgentProfile
from app.db.models.vessel import Vessel
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.incident import Incident, IncidentStatus
from app.db.models.shore_pass import ShorePass
from app.db.models.crew_profile import CrewProfile
from app.db.models.vessel_crew import VesselCrew
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel, EmailStr
from typing import List, Dict, Any

router = APIRouter()

# --- Pydantic Schemas ---

class AgentProfileOut(BaseModel):
    id: int
    name: Optional[str]
    email: EmailStr
    mobile_number: Optional[str]
    agency_name: str
    contact_person: Optional[str]
    location: str
    assigned_port: Optional[str]
    gst_number: Optional[str]
    license_number: Optional[str]
    status: str
    profile_image: Optional[str]
    agent_identifier: Optional[str]

    class Config:
        from_attributes = True

class AgentProfileUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    mobile_number: Optional[str] = None
    agency_name: Optional[str] = None
    contact_person: Optional[str] = None
    location: Optional[str] = None
    assigned_port: Optional[str] = None
    profile_image: Optional[str] = None

class DashboardStats(BaseModel):
    total_vessels: int
    vessels_this_week: int
    crew_in_shore: int
    active_trips: int
    todays_trips: int
    trips_in_progress: int
    open_incidents: int

class DashboardVessel(BaseModel):
    id: int
    name: str
    imo_number: str
    status: str

class DashboardTrip(BaseModel):
    id: int
    crew_name: str
    vessel_name: str
    from_loc: str
    to_loc: str
    status: str

class DashboardData(BaseModel):
    stats: DashboardStats
    active_vessels: List[DashboardVessel]
    live_trips: List[DashboardTrip]

# --- Routes ---

@router.get("/profile", response_model=AgentProfileOut)
def get_agent_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this profile")
    
    agent_profile = current_user.agent_profile
    if not agent_profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    
    return {
        "id": agent_profile.id,
        "name": current_user.name,
        "email": current_user.email,
        "mobile_number": current_user.mobile_number,
        "agency_name": agent_profile.agency_name,
        "contact_person": agent_profile.contact_person,
        "location": agent_profile.location,
        "assigned_port": agent_profile.assigned_port,
        "gst_number": agent_profile.gst_number,
        "license_number": agent_profile.license_number,
        "status": agent_profile.status,
        "profile_image": agent_profile.profile_image,
        "agent_identifier": agent_profile.agent_identifier
    }

@router.patch("/profile", response_model=AgentProfileOut)
def update_agent_profile(
    body: AgentProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can update this profile")
    
    agent_profile = current_user.agent_profile
    if not agent_profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    
    # Update User fields
    if body.name is not None:
        current_user.name = body.name
    if body.email is not None:
        current_user.email = body.email
    if body.mobile_number is not None:
        current_user.mobile_number = body.mobile_number
    
    # Update AgentProfile fields
    if body.agency_name is not None:
        agent_profile.agency_name = body.agency_name
    if body.contact_person is not None:
        agent_profile.contact_person = body.contact_person
    if body.location is not None:
        agent_profile.location = body.location
    if body.assigned_port is not None:
        agent_profile.assigned_port = body.assigned_port
    if body.profile_image is not None:
        agent_profile.profile_image = body.profile_image
        
    db.commit()
    db.refresh(current_user)
    db.refresh(agent_profile)
    
    return get_agent_profile(db, current_user)

@router.get("/dashboard", response_model=DashboardData)
def get_dashboard_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access dashboard data")
    
    # 1. Stats
    vessels_query = db.query(Vessel).filter(Vessel.agent_id == current_user.id)
    total_vessels = vessels_query.count()
    
    # Vessels this week (simple approach: created in last 7 days)
    from datetime import timedelta
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    vessels_this_week = vessels_query.filter(Vessel.created_at >= seven_days_ago).count()
    
    # Active Vessels
    active_vessels_list = vessels_query.filter(Vessel.status == "Active").limit(5).all()
    
    # Crew In Shore (active shore passes for agent's vessels)
    vessel_ids = [v.id for v in vessels_query.all()]
    crew_in_shore = 0
    if vessel_ids:
        # Count active shore passes where crew is on one of agent's vessels
        crew_in_shore = db.query(ShorePass).join(
            CrewProfile, ShorePass.crew_profile_id == CrewProfile.id
        ).join(
            VesselCrew, VesselCrew.hp_id == CrewProfile.passport_number
        ).filter(
            VesselCrew.vessel_id.in_(vessel_ids),
            ShorePass.in_time == None
        ).count()

    # Trips (Cab Bookings)
    # Today's trips
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    todays_trips_query = db.query(CabBooking).filter(CabBooking.created_at >= today_start)
    todays_trips_count = todays_trips_query.count()
    trips_in_progress_count = todays_trips_query.filter(CabBooking.status == BookingStatus.IN_PROGRESS).count()
    
    # Live Trips list (In Progress or Driver Assigned)
    live_trips_data = db.query(CabBooking).filter(
        CabBooking.status.in_([BookingStatus.IN_PROGRESS, BookingStatus.DRIVER_ASSIGNED])
    ).limit(5).all()
    
    # Open Incidents
    open_incidents = db.query(Incident).filter(
        Incident.status.in_([IncidentStatus.ACTIVE, IncidentStatus.INVESTIGATING])
    ).count()

    stats = DashboardStats(
        total_vessels=total_vessels,
        vessels_this_week=vessels_this_week,
        crew_in_shore=crew_in_shore,
        active_trips=crew_in_shore, # Using crew_in_shore as 'active trips' for now as per UI logic
        todays_trips=todays_trips_count,
        trips_in_progress=trips_in_progress_count,
        open_incidents=open_incidents
    )

    return DashboardData(
        stats=stats,
        active_vessels=[
            DashboardVessel(id=v.id, name=v.name, imo_number=v.imo_number, status=v.status)
            for v in active_vessels_list
        ],
        live_trips=[
            DashboardTrip(
                id=t.id,
                crew_name=t.crew.full_name,
                vessel_name=t.vehicle_name, # or t.vessel field if added to CabBooking
                from_loc=t.pickup_address,
                to_loc=t.drop_address,
                status="Active" if t.status == BookingStatus.IN_PROGRESS else "Pending"
            )
            for t in live_trips_data
        ]
    )
