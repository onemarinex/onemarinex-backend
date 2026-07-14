from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, String, or_
from typing import Optional
from datetime import datetime, date as _date

from app.db.session import get_db
from app.db.models.agent_profile import AgentProfile
from app.db.models.vessel import Vessel
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.incident import Incident, IncidentStatus
from app.db.models.shore_pass import ShorePass
from app.db.models.crew_profile import CrewProfile
from app.db.models.vessel_crew import VesselCrew
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.driver import Driver
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel, EmailStr
from typing import List, Dict, Any
from app.api.v1.routes_crew import ShorePassOut

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
    auth_document_url: Optional[str] = None

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
    investigating_incidents: int
    closed_incidents: int

class DashboardVessel(BaseModel):
    id: int
    name: str
    imo_number: str
    status: str
    ongoing_trips_count: int
    crew_ashore_count: int
    incidents_count: int

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

class ShorePassActionIn(BaseModel):
    rejection_reason: Optional[str] = None

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
        "agent_identifier": agent_profile.agent_identifier,
        "auth_document_url": agent_profile.auth_document_url
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
    
    # Active Vessels (Active + Departing — still in port)
    active_vessels_list = vessels_query.filter(Vessel.status.in_(["Active", "Departing"])).limit(5).all()
    
    # Crew In Shore (active shore passes for agent's vessels)
    vessel_ids = [v.id for v in vessels_query.all()]
    crew_in_shore = 0
    if vessel_ids:
        # Count active shore passes where crew is on one of agent's vessels
        crew_in_shore = db.query(ShorePass).join(
            CrewProfile, ShorePass.crew_profile_id == CrewProfile.id
        ).join(
            VesselCrew, VesselCrew.hp_id == CrewProfile.hpid
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
    
    # Open and Closed Incidents for this agent's port
    agent_profile = current_user.agent_profile
    port_incidents_query = db.query(Incident).filter(Incident.port_name == agent_profile.assigned_port if agent_profile else None)
    
    open_incidents = port_incidents_query.filter(
        Incident.status == IncidentStatus.ACTIVE
    ).count()

    investigating_incidents = port_incidents_query.filter(
        Incident.status == IncidentStatus.INVESTIGATING
    ).count()

    closed_incidents = port_incidents_query.filter(
        Incident.status == IncidentStatus.RESOLVED
    ).count()

    stats = DashboardStats(
        total_vessels=total_vessels,
        vessels_this_week=vessels_this_week,
        crew_in_shore=crew_in_shore,
        active_trips=crew_in_shore, # Using crew_in_shore as 'active trips' for now as per UI logic
        todays_trips=todays_trips_count,
        trips_in_progress=trips_in_progress_count,
        open_incidents=open_incidents,
        investigating_incidents=investigating_incidents,
        closed_incidents=closed_incidents
    )

    vessels_data = []
    for v in active_vessels_list:
        # Get crew HPIDs for this vessel
        crew_hpids = [c.hp_id for c in db.query(VesselCrew).filter(VesselCrew.vessel_id == v.id).all() if c.hp_id]
        
        # 1. Ongoing Trips
        ongoing_trips = 0
        if crew_hpids:
            crew_profile_ids = [cp.id for cp in db.query(CrewProfile).filter(CrewProfile.hpid.in_(crew_hpids)).all()]
            if crew_profile_ids:
                ongoing_trips = db.query(CabBooking).filter(
                    CabBooking.crew_id.in_(crew_profile_ids),
                    CabBooking.status.in_([BookingStatus.IN_PROGRESS, BookingStatus.DRIVER_ASSIGNED, BookingStatus.DRIVER_ACCEPTED])
                ).count()
        
        # 2. Crew Ashore
        crew_ashore = 0
        if crew_hpids:
            crew_profile_ids = [cp.id for cp in db.query(CrewProfile).filter(CrewProfile.hpid.in_(crew_hpids)).all()]
            if crew_profile_ids:
                crew_ashore = db.query(ShorePass).filter(
                    ShorePass.crew_profile_id.in_(crew_profile_ids),
                    ShorePass.in_time.is_(None)
                ).count()
                
        # 3. SOS/Incidents of ship
        incidents = 0
        if crew_hpids:
            incidents = db.query(Incident).filter(
                Incident.reporter_id.in_(crew_hpids),
                Incident.status.in_([IncidentStatus.ACTIVE, IncidentStatus.INVESTIGATING])
            ).count()
            
        vessels_data.append(
            DashboardVessel(
                id=v.id,
                name=v.name,
                imo_number=v.imo_number,
                status=v.status,
                ongoing_trips_count=ongoing_trips,
                crew_ashore_count=crew_ashore,
                incidents_count=incidents
            )
        )

    return DashboardData(
        stats=stats,
        active_vessels=vessels_data,
        live_trips=[]
    )

@router.get("/shore-pass-requests", response_model=List[ShorePassOut])
def get_shore_pass_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access shore pass requests")
    
    agent_profile = current_user.agent_profile
    if not agent_profile or not agent_profile.assigned_port:
        return []

    requests = db.query(ShorePass).filter(
        ShorePass.port_name == agent_profile.assigned_port
    ).order_by(ShorePass.created_at.desc()).all()
    
    return requests

@router.post("/shore-pass-requests/{request_id}/approve", response_model=ShorePassOut)
def approve_shore_pass(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can approve shore passes")
    
    shore_pass = db.query(ShorePass).filter(ShorePass.id == request_id).first()
    if not shore_pass:
        raise HTTPException(status_code=404, detail="Shore pass request not found")
    
    shore_pass.status = "approved"
    shore_pass.is_verified = True
    shore_pass.approved_by_id = current_user.id
    
    try:
        db.commit()
        db.refresh(shore_pass)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
    return shore_pass

@router.post("/shore-pass-requests/{request_id}/reject", response_model=ShorePassOut)
def reject_shore_pass(
    request_id: int,
    body: ShorePassActionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can reject shore passes")
    
    shore_pass = db.query(ShorePass).filter(ShorePass.id == request_id).first()
    if not shore_pass:
        raise HTTPException(status_code=404, detail="Shore pass request not found")
    
    shore_pass.status = "rejected"
    shore_pass.rejection_reason = body.rejection_reason
    shore_pass.approved_by_id = current_user.id
    
    try:
        db.commit()
        db.refresh(shore_pass)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
    return shore_pass


# --- Helper: get crew profile IDs for an agent's vessels ---

def _get_agent_crew_profile_ids(db: Session, agent_user_id: int) -> list[int]:
    """Return crew_profile IDs for all crew mapped to the agent's vessels."""
    vessel_ids = [v.id for v in db.query(Vessel).filter(Vessel.agent_id == agent_user_id).all()]
    if not vessel_ids:
        return []
    crew_hpids = [
        c.hp_id for c in db.query(VesselCrew).filter(
            VesselCrew.vessel_id.in_(vessel_ids),
            VesselCrew.hp_id.isnot(None),
        ).all()
        if c.hp_id
    ]
    if not crew_hpids:
        return []
    return [
        cp.id for cp in db.query(CrewProfile).filter(CrewProfile.hpid.in_(crew_hpids)).all()
    ]


def _get_agent_crew_hpids(db: Session, agent_user_id: int) -> list[str]:
    """Return HPIDs for all crew mapped to the agent's vessels."""
    vessel_ids = [v.id for v in db.query(Vessel).filter(Vessel.agent_id == agent_user_id).all()]
    if not vessel_ids:
        return []
    return [
        c.hp_id for c in db.query(VesselCrew).filter(
            VesselCrew.vessel_id.in_(vessel_ids),
            VesselCrew.hp_id.isnot(None),
        ).all()
        if c.hp_id
    ]


# --- Agent Bookings ---

@router.get("/bookings")
def get_agent_bookings(
    status_filter: Optional[str] = None,
    provider_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access agent bookings")

    crew_profile_ids = _get_agent_crew_profile_ids(db, current_user.id)
    if not crew_profile_ids:
        return []

    status_labels = {
        "pending_provider_response": "Pending Provider Response",
        "provider_accepted": "Provider Accepted",
        "provider_rejected": "Provider Rejected",
        "driver_assigned": "Driver Assigned",
        "driver_accepted": "Driver Accepted",
        "on_trip": "On Trip",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "pending": "Pending",
        "confirmed": "Confirmed",
        "arrived": "Arrived",
        "in_progress": "In Progress",
    }
    ride_type_labels = {
        "flexible_ride": "Flexible Ride",
        "guaranteed_coordinated_ride": "Guaranteed Coordinated Ride",
    }

    query = db.query(
        CabBooking.id,
        CabBooking.booking_id,
        cast(CabBooking.ride_type, String).label("ride_type"),
        CabBooking.port,
        CabBooking.pickup_address,
        CabBooking.drop_address,
        cast(CabBooking.vehicle_type, String).label("vehicle_type"),
        CabBooking.vehicle_name,
        CabBooking.vehicle_category,
        CabBooking.estimated_price,
        CabBooking.num_passengers,
        cast(CabBooking.status, String).label("status"),
        CabBooking.provider_id,
        CabBooking.aggregator_id,
        CabBooking.aggregator_name,
        CabBooking.provider_response_status,
        CabBooking.provider_response_at,
        CabBooking.assigned_driver_id,
        CabBooking.driver_id,
        CabBooking.driver_name,
        CabBooking.driver_phone,
        CabBooking.driver_plate,
        CabBooking.driver_assigned_at,
        CabBooking.driver_accepted_at,
        CabBooking.trip_started_at,
        CabBooking.started_at,
        CabBooking.trip_completed_at,
        CabBooking.completed_at,
        CabBooking.otp,
        CabBooking.helpline_number,
        CabBooking.agent_number,
        CabBooking.scheduled_time,
        CabBooking.created_at,
        CabBooking.updated_at,
        CrewProfile.id.label("crew_id"),
        CrewProfile.full_name.label("crew_name"),
        CrewProfile.hpid.label("crew_hpid"),
        CrewProfile.vessel.label("crew_vessel"),
        AggregatorProfile.company_name.label("provider_company_name"),
        AggregatorProfile.provider_type.label("provider_type"),
        Driver.name.label("assigned_driver_name"),
        Driver.phone.label("assigned_driver_phone"),
        Driver.vehicle_number.label("assigned_driver_vehicle_number"),
    )
    query = query.outerjoin(CrewProfile, CabBooking.crew_id == CrewProfile.id)
    query = query.outerjoin(
        AggregatorProfile,
        or_(
            CabBooking.provider_id == AggregatorProfile.id,
            CabBooking.aggregator_id == AggregatorProfile.id,
        ),
    )
    query = query.outerjoin(Driver, CabBooking.assigned_driver_id == Driver.id)

    # Filter to only bookings by crew mapped under this agent
    query = query.filter(CabBooking.crew_id.in_(crew_profile_ids))

    if status_filter:
        query = query.filter(cast(CabBooking.status, String) == status_filter.lower())
    if provider_type:
        query = query.filter(AggregatorProfile.provider_type == provider_type)
    if date_from:
        query = query.filter(CabBooking.created_at >= date_from)
    if date_to:
        query = query.filter(CabBooking.created_at <= date_to)

    bookings = query.order_by(CabBooking.created_at.desc()).all()

    response: List[Dict[str, Any]] = []
    for booking in bookings:
        status_value = (booking.status or "").lower() if booking.status else None
        ride_type_value = (booking.ride_type or "").lower() if booking.ride_type else None
        response.append(
            {
                "id": booking.id,
                "booking_id": booking.booking_id,
                "ride_type": ride_type_value,
                "ride_type_label": ride_type_labels.get(ride_type_value),
                "port": booking.port,
                "crew": {
                    "id": booking.crew_id,
                    "name": booking.crew_name,
                    "hp_id": booking.crew_hpid,
                    "vessel": booking.crew_vessel,
                },
                "pickup_address": booking.pickup_address,
                "drop_address": booking.drop_address,
                "vehicle_type": (booking.vehicle_type or "").lower() if booking.vehicle_type else None,
                "vehicle_name": booking.vehicle_name,
                "vehicle_category": booking.vehicle_category,
                "estimated_price": float(booking.estimated_price) if booking.estimated_price else 0,
                "num_passengers": booking.num_passengers,
                "status": status_value,
                "status_label": status_labels.get(status_value, booking.status),
                "provider_id": booking.provider_id or booking.aggregator_id,
                "provider_name": booking.provider_company_name or booking.aggregator_name,
                "provider_type": booking.provider_type,
                "provider_response_status": booking.provider_response_status,
                "provider_response_at": booking.provider_response_at,
                "assigned_driver_id": booking.assigned_driver_id or booking.driver_id,
                "driver_name": booking.driver_name or booking.assigned_driver_name,
                "driver_phone": booking.driver_phone or booking.assigned_driver_phone,
                "driver_plate": booking.driver_plate or booking.assigned_driver_vehicle_number,
                "driver_assigned_at": booking.driver_assigned_at,
                "driver_accepted_at": booking.driver_accepted_at,
                "trip_started_at": booking.trip_started_at or booking.started_at,
                "trip_completed_at": booking.trip_completed_at or booking.completed_at,
                "otp": booking.otp,
                "helpline_number": booking.helpline_number or booking.agent_number,
                "scheduled_time": booking.scheduled_time,
                "created_at": booking.created_at,
                "updated_at": booking.updated_at,
            }
        )
    return response


# --- Agent: View Crew by HPID ---

class AgentCrewDetailOut(BaseModel):
    id: int
    full_name: str
    rank: str
    nationality: Optional[str]
    passport_number: Optional[str]
    date_of_birth: Optional[_date]
    hpid: Optional[str]
    current_port: Optional[str]
    vessel: Optional[str]
    vessel_id: Optional[int] = None
    vessel_name: Optional[str] = None
    imo_number: Optional[str] = None
    vessel_type: Optional[str] = None
    status: str = "Unmapped"
    shore_pass_eligible: bool = False
    expiry_date: Optional[_date] = None
    mapping_status: Optional[str] = None
    shore_pass_status: Optional[str] = None
    shore_pass_id: Optional[int] = None
    shore_pass_out_time: Optional[datetime] = None
    shore_pass_in_time: Optional[datetime] = None
    shore_pass_expires_at: Optional[datetime] = None

    class Config:
        from_attributes = True


@router.get("/crew/{hp_id}", response_model=AgentCrewDetailOut)
def get_agent_crew_detail(
    hp_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this")

    # Find the VesselCrew record for this HPID on one of the agent's vessels
    vessel_crew = db.query(VesselCrew).filter(VesselCrew.hp_id == hp_id).first()
    vessel = None
    if vessel_crew:
        vessel = db.query(Vessel).filter(
            Vessel.id == vessel_crew.vessel_id,
            Vessel.agent_id == current_user.id
        ).first()
    if not vessel_crew or not vessel:
        raise HTTPException(status_code=404, detail="Crew not found in your vessels")

    crew_profile = db.query(CrewProfile).filter(CrewProfile.hpid == hp_id).first()

    shore_pass = None
    if crew_profile:
        shore_pass = db.query(ShorePass).filter(
            ShorePass.crew_profile_id == crew_profile.id
        ).order_by(ShorePass.created_at.desc()).first()

    full_name = crew_profile.full_name if crew_profile else vessel_crew.name
    rank = crew_profile.rank if crew_profile else vessel_crew.rank
    nationality = crew_profile.nationality if crew_profile else vessel_crew.nationality
    passport_number = crew_profile.passport_number if crew_profile else None
    date_of_birth = crew_profile.date_of_birth if crew_profile else None

    return AgentCrewDetailOut(
        id=crew_profile.id if crew_profile else vessel_crew.id,
        full_name=full_name,
        rank=rank,
        nationality=nationality,
        passport_number=passport_number,
        date_of_birth=date_of_birth,
        hpid=hp_id,
        current_port=crew_profile.current_port if crew_profile else None,
        vessel=crew_profile.vessel if crew_profile else vessel.name,
        vessel_id=vessel_crew.vessel_id,
        vessel_name=vessel.name,
        imo_number=vessel.imo_number,
        vessel_type=vessel.vessel_type,
        status=vessel_crew.status,
        shore_pass_eligible=vessel_crew.shore_pass_eligible,
        expiry_date=vessel_crew.expiry_date,
        mapping_status=vessel_crew.status,
        shore_pass_status=shore_pass.status if shore_pass else None,
        shore_pass_id=shore_pass.id if shore_pass else None,
        shore_pass_out_time=shore_pass.out_time if shore_pass else None,
        shore_pass_in_time=shore_pass.in_time if shore_pass else None,
        shore_pass_expires_at=shore_pass.expires_at if shore_pass else None,
    )
