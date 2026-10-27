import io
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, cast, String, or_
from typing import Optional
from datetime import datetime, timedelta, date as _date, timezone as dt_timezone

from app.db.session import get_db
from app.services.assignment_ref import resolve_crew_identifier
from app.db.models.agent_profile import AgentProfile
from app.db.models.vessel import Vessel
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.incident import Incident, IncidentStatus
from app.db.models.shore_pass import ShorePass
from app.db.models.crew_sos import CrewSos
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel_crew import VesselCrew
from app.db.models.vessel_call import VesselCall
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.driver import Driver
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel, EmailStr, Field
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
    agency_logo_url: Optional[str] = None
    support_number: Optional[str] = None
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
    # Unresolved SOS alerts from this agent's crew. Defaulted so an older
    # frontend build that does not know the field still parses the response.
    open_sos: int = 0

class DashboardVessel(BaseModel):
    id: int
    name: str
    imo_number: str
    status: str
    ongoing_trips_count: int
    crew_ashore_count: int
    # How much of that count rests on evidence, and how much does not.
    # Audit R05: the figure accepts open passes and started trips, and a
    # started trip with no reach event says nothing about where anyone is.
    # Reporting the count alone presented an estimate as a measurement.
    crew_ashore_unknown_seats: int = 0
    crew_ashore_unknown_trips: int = 0
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


# A trip that is happening right now. ON_TRIP/ARRIVED are the current statuses;
# IN_PROGRESS is the legacy spelling kept on older rows.
LIVE_TRIP_STATUSES = [
    BookingStatus.DRIVER_ASSIGNED,
    BookingStatus.DRIVER_ACCEPTED,
    BookingStatus.ARRIVED,
    BookingStatus.ON_TRIP,
    BookingStatus.IN_PROGRESS,
]


def _agent_scope(db: Session, agent_user_id: int) -> tuple[list[int], list[int]]:
    """Vessels this agent owns, and the crew profiles sailing on them.

    Everything on the agent dashboard must be limited to the agent's own ships.
    Crew are matched by HPID, which is the only link between a vessel's manifest
    (``vessel_crew``) and a registered crew account (``crew_profiles``).
    """
    vessel_ids = [v.id for v in db.query(Vessel.id).filter(Vessel.agent_id == agent_user_id).all()]
    if not vessel_ids:
        return [], []

    hp_ids = [
        c.hp_id
        for c in db.query(VesselCrew.hp_id).filter(VesselCrew.vessel_id.in_(vessel_ids)).all()
        if c.hp_id
    ]
    if not hp_ids:
        return vessel_ids, []

    crew_profile_ids = [
        cp.id for cp in db.query(CrewProfile.id).filter(CrewProfile.hpid.in_(hp_ids)).all()
    ]
    return vessel_ids, crew_profile_ids


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
        "agency_logo_url": agent_profile.agency_logo_url,
        "agency_logo_display_url": _agency_logo_display_url(agent_profile),
        "support_number": agent_profile.support_number,
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

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@router.post("/profile/image", response_model=AgentProfileOut)
async def upload_agent_image(
    kind: str = "profile",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload the agent's profile picture, or the agency logo.

    `kind=profile` sets the contact person's avatar; `kind=logo` sets the
    agency logo used on the PDF reports. They are separate images and are
    stored in separate columns.

    There was previously no upload route at all, so the "Change Profile"
    button on the profile page had nothing to call.
    """
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can update this profile")

    agent_profile = current_user.agent_profile
    if not agent_profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")

    if kind not in {"profile", "logo"}:
        raise HTTPException(status_code=400, detail="kind must be 'profile' or 'logo'")

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Upload a JPEG, PNG, WebP or GIF image.",
        )

    # Read once so the size can be checked before anything is stored.
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller.")
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    from app.services.storage import save_fileobj

    suffix = (file.filename or "").rsplit(".", 1)[-1].lower() or "jpg"
    key = f"agent_{kind}/{agent_profile.id}_{uuid.uuid4().hex[:8]}.{suffix}"
    stored = save_fileobj(io.BytesIO(data), key, content_type=file.content_type, public=True)

    if kind == "logo":
        agent_profile.agency_logo_url = stored
    else:
        agent_profile.profile_image = stored

    db.commit()
    db.refresh(agent_profile)
    return get_agent_profile(db, current_user)


@router.get("/dashboard", response_model=DashboardData)
def get_dashboard_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access dashboard data")
    
    # Resolve lifecycle from backend time before any status-based query.
    from app.services.vessel_lifecycle import (
        belongs_to_call,
        current_call_for,
        synchronize_vessel_lifecycle,
    )

    owned_vessels = db.query(Vessel).filter(Vessel.agent_id == current_user.id).all()
    if synchronize_vessel_lifecycle(db, owned_vessels):
        db.commit()

    # 1. Stats
    vessels_query = db.query(Vessel).filter(Vessel.agent_id == current_user.id)
    total_vessels = vessels_query.filter(
        Vessel.status.in_(["Active", "Departing"])
    ).count()
    
    # Vessels this week (simple approach: created in last 7 days)
    from datetime import timedelta
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    vessels_this_week = vessels_query.filter(Vessel.created_at >= seven_days_ago).count()
    
    # Active Vessels (Active + Departing — still in port)
    active_vessels_list = vessels_query.filter(Vessel.status.in_(["Active", "Departing"])).limit(5).all()
    
    # Every tile below is limited to this agent's own ships. Anything unscoped
    # reports the whole platform's activity, which is what made Crew Ashore and
    # Active Trips show the same number for every agent.
    vessel_ids, crew_profile_ids = _agent_scope(db, current_user.id)
    agency_profile_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == current_user.id
    ).scalar()

    def _current_event_scope(model, *, legacy_identity_clause=None):
        """Prefer immutable event ownership; isolate legacy identity fallback."""
        clauses = []
        if agency_profile_id is not None and vessel_ids:
            clauses.append(and_(
                model.agency_id == agency_profile_id,
                model.vessel_id.in_(vessel_ids),
            ))
        legacy = [model.agency_id.is_(None)]
        if vessel_ids:
            legacy.append(model.vessel_id.in_(vessel_ids))
        if legacy_identity_clause is not None:
            legacy.append(and_(model.vessel_id.is_(None), legacy_identity_clause))
        if len(legacy) > 1:
            clauses.append(and_(legacy[0], or_(*legacy[1:])))
        return or_(*clauses) if clauses else None

    # Crew ashore: they have actually left the ship and not come back.
    #
    # Shared with the shore leave report, which recognises a started cab trip as
    # evidence too. Counting open shore passes alone read zero while a trip was
    # underway with crew off the ship, and counted pass rows rather than people.
    from app.services.crew_ashore import crew_ashore_count

    crew_in_shore = crew_ashore_count(db, crew_profile_ids)

    # Trips (Cab Bookings) — this agent's crew only.
    # "Today" is the port's day, not UTC's: UTC midnight is 05:30 IST, so this
    # counted from yesterday afternoon and reset mid-morning.
    from app.services.port_time import agent_port_day
    today_start, today_end, _ = agent_port_day(db, current_user)
    todays_trips_count = 0
    trips_in_progress_count = 0
    active_trips_count = 0
    live_trips_data = []
    trip_scope = _current_event_scope(
        CabBooking,
        legacy_identity_clause=(
            CabBooking.crew_id.in_(crew_profile_ids) if crew_profile_ids else None
        ),
    )
    if trip_scope is not None:
        agent_trips = db.query(CabBooking).filter(trip_scope)
        todays_trips_count = agent_trips.filter(
            CabBooking.created_at >= today_start,
            CabBooking.created_at < today_end,
        ).count()
        trips_in_progress_count = agent_trips.filter(
            CabBooking.created_at >= today_start,
            CabBooking.created_at < today_end,
            CabBooking.status.in_(LIVE_TRIP_STATUSES),
        ).count()
        # "Active trips" is every trip underway right now, regardless of the day
        # it was booked — an overnight trip is still active this morning.
        active_trips_count = agent_trips.filter(CabBooking.status.in_(LIVE_TRIP_STATUSES)).count()
        live_trips_data = agent_trips.filter(
            CabBooking.status.in_(LIVE_TRIP_STATUSES)
        ).order_by(CabBooking.created_at.desc(), CabBooking.id.desc()).limit(5).all()

    # Safety records are historical events. Never infer ownership from whoever
    # the crew member is assigned to now: unresolved legacy rows stay available
    # to superadmins for reconciliation, but are not exposed to an agent.
    open_incidents = investigating_incidents = closed_incidents = 0
    incident_scope = (
        and_(Incident.agency_id == agency_profile_id, Incident.vessel_id.in_(vessel_ids))
        if agency_profile_id is not None and vessel_ids else None
    )
    if incident_scope is not None:
        agent_incidents = db.query(Incident).filter(incident_scope)
        open_incidents = agent_incidents.filter(Incident.status == IncidentStatus.ACTIVE).count()
        investigating_incidents = agent_incidents.filter(
            Incident.status == IncidentStatus.INVESTIGATING
        ).count()
        closed_incidents = agent_incidents.filter(
            Incident.status == IncidentStatus.RESOLVED
        ).count()

    # The tile is labelled "Open SOS/Incidents", so owned open SOS alerts are
    # included beside incidents. Unresolved legacy alerts stay superadmin-only.
    open_sos = 0
    sos_scope = (
        and_(CrewSos.agency_id == agency_profile_id, CrewSos.vessel_id.in_(vessel_ids))
        if agency_profile_id is not None and vessel_ids else None
    )
    if sos_scope is not None:
        open_sos = db.query(CrewSos).filter(
            sos_scope,
            CrewSos.closed_at.is_(None),
            CrewSos.cancelled_at.is_(None),
        ).count()

    stats = DashboardStats(
        total_vessels=total_vessels,
        vessels_this_week=vessels_this_week,
        crew_in_shore=crew_in_shore,
        active_trips=active_trips_count,
        todays_trips=todays_trips_count,
        trips_in_progress=trips_in_progress_count,
        open_incidents=open_incidents,
        investigating_incidents=investigating_incidents,
        closed_incidents=closed_incidents,
        open_sos=open_sos,
    )

    vessels_data = []
    for v in active_vessels_list:
        current_call_id = db.query(VesselCall.id).filter(
            VesselCall.vessel_id == v.id,
            VesselCall.ended_at.is_(None),
        ).scalar()
        # Get crew HPIDs for this vessel
        crew_hpids = [c.hp_id for c in db.query(VesselCrew).filter(VesselCrew.vessel_id == v.id).all() if c.hp_id]

        vessel_crew_ids = []
        if crew_hpids:
            vessel_crew_ids = [
                cp.id for cp in db.query(CrewProfile.id).filter(CrewProfile.hpid.in_(crew_hpids)).all()
            ]

        # 1. Ongoing Trips
        ongoing_trips = 0
        vessel_trip_scope = or_(
            CabBooking.vessel_call_id == current_call_id
            if current_call_id is not None else False,
            and_(
                CabBooking.vessel_call_id.is_(None),
                or_(
                    CabBooking.vessel_id == v.id,
                    and_(
                        CabBooking.vessel_id.is_(None),
                        CabBooking.crew_id.in_(vessel_crew_ids),
                    ) if vessel_crew_ids else False,
                ),
            ),
        )
        if current_call_id is not None or vessel_crew_ids:
            ongoing_trips = db.query(CabBooking).filter(
                vessel_trip_scope,
                CabBooking.status.in_(LIVE_TRIP_STATUSES),
            ).count()

        # 2. Crew Ashore — same calculation as the headline tile above, but
        # keeping the coverage the count throws away (audit R05).
        from app.services.crew_ashore import crew_ashore_breakdown

        ashore = crew_ashore_breakdown(db, vessel_crew_ids)
        crew_ashore = ashore.count

        # 3. SOS/Incidents of ship — the card says "SOS/Incidents", so count both.
        #
        # Of the call the ship is on, not of the hull for all time. The trips
        # above were already scoped this way and these were not, so a returning
        # vessel's card reported the previous call's open records while the
        # Incidents tab beside it — scoped since the vessel-page fix — read
        # zero. MV JIM MING 82 arrived on a new call showing 1 SOS/incident it
        # had never had.
        incidents = 0
        if agency_profile_id is not None:
            call = current_call_for(db, v.id)
            incidents = db.query(Incident).filter(
                Incident.agency_id == agency_profile_id,
                belongs_to_call(
                    Incident.vessel_call_id, Incident.vessel_id, Incident.created_at,
                    vessel_id=v.id, call=call,
                ),
                Incident.status.in_([IncidentStatus.ACTIVE, IncidentStatus.INVESTIGATING]),
            ).count()
            incidents += db.query(CrewSos).filter(
                CrewSos.agency_id == agency_profile_id,
                belongs_to_call(
                    CrewSos.vessel_call_id, CrewSos.vessel_id, CrewSos.created_at,
                    vessel_id=v.id, call=call,
                ),
                CrewSos.closed_at.is_(None),
                CrewSos.cancelled_at.is_(None),
            ).count()

        vessels_data.append(
            DashboardVessel(
                id=v.id,
                name=v.name,
                imo_number=v.imo_number,
                status=v.status,
                ongoing_trips_count=ongoing_trips,
                crew_ashore_count=crew_ashore,
                crew_ashore_unknown_seats=ashore.unknown_seats,
                crew_ashore_unknown_trips=ashore.unknown_bookings,
                incidents_count=incidents
            )
        )

    # live_trips was previously computed and then discarded, so the dashboard's
    # live trips list was always empty. Resolve crew and vessel names for it.
    trips_data = []
    if live_trips_data:
        trip_crew_ids = {b.crew_id for b in live_trips_data if b.crew_id}
        crew_rows = db.query(CrewProfile).filter(CrewProfile.id.in_(trip_crew_ids)).all() if trip_crew_ids else []
        crew_by_id = {cp.id: cp for cp in crew_rows}

        trip_vessel_ids = {b.vessel_id for b in live_trips_data if b.vessel_id}
        vessel_name_by_id = dict(
            db.query(Vessel.id, Vessel.name).filter(Vessel.id.in_(trip_vessel_ids)).all()
        ) if trip_vessel_ids else {}
        trip_call_ids = {b.vessel_call_id for b in live_trips_data if b.vessel_call_id}
        vessel_name_by_call = dict(
            db.query(VesselCall.id, VesselCall.vessel_name).filter(
                VesselCall.id.in_(trip_call_ids)
            ).all()
        ) if trip_call_ids else {}
        # Only unstamped legacy bookings need the current-manifest fallback.
        # Keep it only when one HPID maps to one owned vessel; ambiguity is
        # shown as unknown rather than moving a historical trip to a new ship.
        legacy_vessels_by_hpid = {}
        hpids = [cp.hpid for cp in crew_rows if cp.hpid]
        if hpids:
            candidates = {}
            for hpid, vessel_name in (
                db.query(VesselCrew.hp_id, Vessel.name)
                .join(Vessel, Vessel.id == VesselCrew.vessel_id)
                .filter(VesselCrew.hp_id.in_(hpids), Vessel.agent_id == current_user.id)
                .all()
            ):
                candidates.setdefault(hpid, set()).add(vessel_name)
            legacy_vessels_by_hpid = {
                hpid: next(iter(names))
                for hpid, names in candidates.items()
                if len(names) == 1
            }

        for b in live_trips_data:
            crew = crew_by_id.get(b.crew_id)
            trips_data.append(
                DashboardTrip(
                    id=b.id,
                    crew_name=(crew.full_name if crew else None) or "Unknown crew",
                    vessel_name=(
                        vessel_name_by_call.get(b.vessel_call_id)
                        or vessel_name_by_id.get(b.vessel_id)
                        or legacy_vessels_by_hpid.get(crew.hpid if crew else None)
                        or "—"
                    ),
                    from_loc=b.pickup_address or "—",
                    to_loc=b.drop_address or "—",
                    status=b.status.value if hasattr(b.status, "value") else str(b.status),
                )
            )

    return DashboardData(
        stats=stats,
        active_vessels=vessels_data,
        live_trips=trips_data,
    )

@router.get("/shore-pass-requests", response_model=List[ShorePassOut])
def get_shore_pass_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access shore pass requests")
    
    # Scoped to the agent's own vessels. Filtering by port showed — and let the
    # agent act on — shore passes belonging to every other agency berthed at
    # the same port.
    _, crew_profile_ids = _agent_scope(db, current_user.id)
    if not crew_profile_ids:
        return []

    requests = db.query(ShorePass).filter(
        ShorePass.crew_profile_id.in_(crew_profile_ids)
    ).order_by(ShorePass.created_at.desc()).all()

    return requests


def _agent_shore_pass_or_404(db: Session, request_id: int, current_user: User) -> ShorePass:
    """A shore pass belonging to this agent's crew, or a 404.

    Approve and reject previously looked the pass up by id with no ownership
    check at all, so any agent could grant or refuse shore leave for another
    agency's crew. 404 rather than 403 so ids cannot be probed.
    """
    shore_pass = db.query(ShorePass).filter(ShorePass.id == request_id).first()
    if not shore_pass:
        raise HTTPException(status_code=404, detail="Shore pass request not found")

    _, crew_profile_ids = _agent_scope(db, current_user.id)
    if shore_pass.crew_profile_id not in crew_profile_ids:
        raise HTTPException(status_code=404, detail="Shore pass request not found")
    return shore_pass

@router.post("/shore-pass-requests/{request_id}/approve", response_model=ShorePassOut)
def approve_shore_pass(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can approve shore passes")

    shore_pass = _agent_shore_pass_or_404(db, request_id, current_user)
    
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

    shore_pass = _agent_shore_pass_or_404(db, request_id, current_user)
    
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
    agency_profile_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == current_user.id
    ).scalar()
    if agency_profile_id is None and not crew_profile_ids:
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
        func.coalesce(CrewAssignment.crew_name, CrewProfile.full_name).label("crew_name"),
        func.coalesce(CrewAssignment.hpid, CrewProfile.hpid).label("crew_hpid"),
        VesselCall.vessel_name.label("crew_vessel"),
        AggregatorProfile.company_name.label("provider_company_name"),
        AggregatorProfile.provider_type.label("provider_type"),
        Driver.name.label("assigned_driver_name"),
        Driver.phone.label("assigned_driver_phone"),
        Driver.vehicle_number.label("assigned_driver_vehicle_number"),
    )
    query = query.outerjoin(CrewProfile, CabBooking.crew_id == CrewProfile.id)
    query = query.outerjoin(
        CrewAssignment, CabBooking.crew_assignment_id == CrewAssignment.id
    )
    query = query.outerjoin(VesselCall, CabBooking.vessel_call_id == VesselCall.id)
    query = query.outerjoin(
        AggregatorProfile,
        or_(
            CabBooking.provider_id == AggregatorProfile.id,
            CabBooking.aggregator_id == AggregatorProfile.id,
        ),
    )
    query = query.outerjoin(Driver, CabBooking.assigned_driver_id == Driver.id)

    ownership = []
    if agency_profile_id is not None:
        ownership.append(CabBooking.agency_id == agency_profile_id)

    # The legacy arm, aligned with `_agent_may_view_booking`, which is the
    # rule the audit already examined under R04.
    #
    # This list used to ask only "is the agency null and is this crew member on
    # one of my current manifests", with no condition on the booking's own
    # vessel. So a booking for a ship this agency has never operated appeared
    # here -- including its ride OTP -- while opening that same booking by id
    # was refused, because the detail check requires the stored vessel to be
    # one of theirs. Narrowing the list to the detail rule removes the
    # inconsistency without removing the fallback, which the audit says must
    # not happen before its impact is measured.
    agent_vessel_ids = [
        row[0] for row in db.query(Vessel.id).filter(Vessel.agent_id == current_user.id).all()
    ]
    legacy_alternatives = []
    if agent_vessel_ids:
        legacy_alternatives.append(CabBooking.vessel_id.in_(agent_vessel_ids))
    if crew_profile_ids:
        # Only where the booking records no vessel of its own; a booking that
        # names someone else's ship is not theirs because the passenger is.
        legacy_alternatives.append(and_(
            CabBooking.vessel_id.is_(None),
            CabBooking.crew_id.in_(crew_profile_ids),
        ))
    if legacy_alternatives:
        ownership.append(and_(
            CabBooking.agency_id.is_(None),
            or_(*legacy_alternatives),
        ))

    if not ownership:
        return []
    query = query.filter(or_(*ownership))

    if status_filter:
        query = query.filter(cast(CabBooking.status, String) == status_filter.lower())
    if provider_type:
        query = query.filter(AggregatorProfile.provider_type == provider_type)
    if date_from:
        query = query.filter(CabBooking.created_at >= date_from)
    if date_to:
        query = query.filter(CabBooking.created_at <= date_to)

    bookings = query.order_by(CabBooking.created_at.desc(), CabBooking.id.desc()).all()

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
    # The opaque per-assignment reference. The agent screens show this; hpid
    # stays in the payload for anything still keyed on the legacy value.
    reference: Optional[str] = None
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
    vessel_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this")

    # Accepts the opaque reference or the legacy identifier. Translation only:
    # the agent-ownership filter below is unchanged and still does the work.
    hp_id = resolve_crew_identifier(db, hp_id)

    query = (
        db.query(VesselCrew, Vessel)
        .join(Vessel, Vessel.id == VesselCrew.vessel_id)
        .filter(
            func.upper(func.trim(VesselCrew.hp_id)) == hp_id.strip().upper(),
            Vessel.agent_id == current_user.id,
        )
    )
    if vessel_id is not None:
        query = query.filter(Vessel.id == vessel_id)
    matches = query.order_by(VesselCrew.id.desc()).all()
    if not matches:
        raise HTTPException(status_code=404, detail="Crew not found in your vessels")
    if len({vessel.id for _, vessel in matches}) > 1:
        raise HTTPException(
            status_code=409,
            detail="Crew belongs to multiple vessels; select a vessel",
        )
    vessel_crew, vessel = matches[0]

    crew_profile = db.query(CrewProfile).filter(
        func.upper(func.trim(CrewProfile.hpid)) == hp_id.strip().upper()
    ).first()

    shore_pass = None
    assignment = None
    if crew_profile:
        assignment = (
            db.query(CrewAssignment)
            .filter(
                CrewAssignment.vessel_crew_id == vessel_crew.id,
                CrewAssignment.crew_profile_id == crew_profile.id,
            )
            .order_by(CrewAssignment.started_at.desc(), CrewAssignment.id.desc())
            .first()
        )
        if assignment:
            shore_pass = db.query(ShorePass).filter(
                ShorePass.crew_profile_id == crew_profile.id,
                ShorePass.crew_assignment_id == assignment.id,
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
        reference=assignment.public_ref if assignment else None,
        current_port=crew_profile.current_port if crew_profile else None,
        vessel=vessel.name,
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


# --- Reports (K) -----------------------------------------------------------

class ShoreLeaveReportOut(BaseModel):
    """Everything the Shore Leave Operation Report prints.

    Rendered and printed in the browser rather than generated server-side: no
    new dependency, nothing to host, and the preview the agent checks is the
    exact thing that prints. The stamp and signature are added by hand, so the
    layout leaves space rather than trying to reproduce them.
    """
    vessel_name: str
    imo_number: Optional[str] = None
    berth: Optional[str] = None
    port_name: Optional[str] = None
    flag: Optional[str] = None
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    agency_name: Optional[str] = None
    agency_logo_url: Optional[str] = None
    report_date: str
    # The span the figures cover, on the port's clock. `report_date` stays the
    # last day of it, so a sheet printed before this existed still reads the
    # same and frozen snapshots keep their meaning.
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    generated_at: datetime

    crew_onboard: int
    eligible_for_shore_leave: int
    crew_went_ashore: int
    # Share of the crew who were allowed ashore and actually went. Computed
    # server-side so the report and any other reader agree.
    shore_leave_utilisation_pct: int = 0
    completed_trips: int
    average_duration_minutes: Optional[float] = None
    returned_safely: int
    still_ashore: int
    sos_raised: int
    incidents_reported: int
    incidents_resolved: int
    # Resolved SOS alerts and resolved incidents together. `outstanding_issues`
    # has always counted both as open, so reporting only resolved *incidents*
    # beside it left the two halves of the same ledger disagreeing.
    #
    # `incidents_resolved` is kept as it was so report snapshots frozen before
    # this still read correctly; only the printed tile moved to the pair.
    safety_resolved: int = 0
    outstanding_issues: int

    all_returned: bool
    incidents: List[Dict[str, Any]] = []


def _resolve_report_call(db, current_user, vessel_id, vessel_call_id):
    """The vessel and call a report may cover, scoped to the signed-in agency.

    Shared by the report and by the calendar beside it so the two cannot answer
    to different scoping rules — a duplicated copy of this is how one agency
    ends up reading another's call.

    Returns the agent profile, vessel, call, and whether the caller named an
    exact call: a named call reports only what belongs to it, while the
    fallback also sweeps in records that predate call ownership.
    """
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can generate reports")

    agent_profile = db.query(AgentProfile).filter(
        AgentProfile.user_id == current_user.id
    ).first()
    if not agent_profile:
        raise HTTPException(status_code=403, detail="Agent profile not found")

    strict_call_scope = vessel_call_id is not None
    call = None
    if vessel_call_id is not None:
        call = db.query(VesselCall).filter(
            VesselCall.id == vessel_call_id,
            VesselCall.vessel_id == vessel_id,
            VesselCall.agency_id == agent_profile.id,
        ).first()
        if not call:
            raise HTTPException(status_code=404, detail="Vessel call not found")
        vessel = db.query(Vessel).filter(Vessel.id == call.vessel_id).first()
    else:
        vessel = db.query(Vessel).filter(
            Vessel.id == vessel_id, Vessel.agent_id == current_user.id
        ).first()
        if vessel:
            call = db.query(VesselCall).filter(
                VesselCall.vessel_id == vessel.id,
                VesselCall.agency_id == agent_profile.id,
                VesselCall.ended_at.is_(None),
            ).order_by(VesselCall.id.desc()).first()
            if call is None:
                from app.services.historical_context import active_vessel_call

                call = active_vessel_call(db, vessel)
    if not vessel or not call:
        raise HTTPException(status_code=404, detail="Vessel call not found")
    return agent_profile, vessel, call, strict_call_scope


def _call_reporting_window(call):
    """From the ship's arrival to now, or to its departure once it has sailed.

    An agent sends this report when the vessel leaves, so it has to describe the
    whole port call: generated on the 20th for a call that ran the 10th to the
    15th, it covers those six days and stops there.

    The start is the *earliest* of the arrival, the moment the call opened and
    the row's creation, rather than the ETA alone. Those normally agree; when
    they do not, taking the earliest is what guarantees no record falls before
    the window and silently vanishes from the report.

    The end is capped both ways — never past the departure, never into the
    future — so a call still alongside reports up to this moment and a departed
    one stops at its ETD.
    """
    now = datetime.now(dt_timezone.utc)

    def _aware(value):
        if value is None:
            return None
        return value.replace(tzinfo=dt_timezone.utc) if value.tzinfo is None else value

    candidates = [_aware(call.eta), _aware(call.started_at), _aware(call.created_at)]
    start = min([value for value in candidates if value is not None], default=None)

    finished = _aware(call.ended_at) or _aware(call.etd)
    end = min(finished, now) if finished is not None else now
    if start is None:
        start = end
    if end < start:
        # A call whose dates disagree still has to report something rather than
        # an empty window that would read as "nothing happened".
        end = now if now >= start else start
    return start, end


@router.get("/reports/shore-leave/{vessel_id}", response_model=ShoreLeaveReportOut)
def shore_leave_report(
    vessel_id: int,
    vessel_call_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everything one vessel's shore leave has amounted to so far this call.

    The report used to cover a single calendar day, which is not what an agent
    sends. They send it once, when the ship leaves, and it has to account for
    the whole port call — so a report generated on the 20th of a call that ran
    the 10th to the 15th describes those six days, not the 20th.

    Reporting one day also made every quiet date print a sheet of zeros
    indistinguishable from a broken report, which is what "data is not being
    fetched" turned out to mean: the trips were on a day nobody had selected.
    """
    agent_profile, vessel, call, strict_call_scope = _resolve_report_call(
        db, current_user, vessel_id, vessel_call_id
    )

    period_start, period_end = _call_reporting_window(call)
    day_start, day_end = period_start, period_end

    # The printed dates are the port's calendar, not UTC's — the same clock the
    # ETA and ETD on the sheet are shown against.
    from app.db.models.port_rule import PortRule
    from app.services.port_time import as_port_local, resolve_port_timezone

    _configured = db.query(PortRule.timezone).filter(
        PortRule.port_name == (call.port_name or agent_profile.assigned_port)
    ).first()
    _zone_name, _zone = resolve_port_timezone(
        call.port_name or agent_profile.assigned_port,
        _configured[0] if _configured else None,
    )
    period_start_label = as_port_local(period_start, _zone).date().isoformat()
    period_end_label = as_port_local(period_end, _zone).date().isoformat()
    resolved_date = period_end_label

    # The manifest decides who is aboard; an account is only how their own
    # records are found. Counting by crew profile alone dropped anyone who had
    # never registered — so a cab booked for three read as one person ashore.
    from app.services import crew_linkage

    roster = crew_linkage.vessel_call_roster(db, call)
    crew_profile_ids = roster.profile_ids
    eligible_keys = roster.eligible_keys

    # Shore passes predate vessel-call ownership. Use them only for the current
    # call; historical reports must not guess which concurrent assignment a
    # profile-level pass belonged to.
    passes = db.query(ShorePass).filter(
        ShorePass.crew_profile_id.in_(crew_profile_ids),
        ShorePass.out_time >= day_start,
        ShorePass.out_time < day_end,
    ).all() if crew_profile_ids and call.ended_at is None else []

    # A trip belongs to the day it ran. That is what a shore pass's out_time
    # already means, and anchoring trips on created_at instead disagreed with
    # it: a cab booked at 23:50 and driven after midnight landed wholly on the
    # earlier day, while one booked the night before and driven this morning
    # was left out of this day entirely.
    #
    # A trip that never started put nobody ashore and has no running time, so
    # it stays on the day it was booked rather than disappearing from the count.
    trip_start = func.coalesce(CabBooking.trip_started_at, CabBooking.started_at)
    booking_scope = [CabBooking.vessel_call_id == call.id]
    if not strict_call_scope:
        booking_scope.append(CabBooking.vessel_id == vessel.id)
        if crew_profile_ids:
            booking_scope.append(and_(
                CabBooking.vessel_call_id.is_(None),
                CabBooking.vessel_id.is_(None),
                CabBooking.crew_id.in_(crew_profile_ids),
            ))
    day_trips = db.query(CabBooking).filter(
        or_(*booking_scope),
        or_(
            and_(trip_start.isnot(None), trip_start >= day_start, trip_start < day_end),
            and_(
                trip_start.is_(None),
                CabBooking.created_at >= day_start,
                CabBooking.created_at < day_end,
            ),
        ),
    ).all()

    assignment_ids = {t.crew_assignment_id for t in day_trips if t.crew_assignment_id}
    assignments = {
        row.id: row
        for row in db.query(CrewAssignment).filter(
            CrewAssignment.id.in_(assignment_ids)
        ).all()
    } if assignment_ids else {}

    def _trip_crew(trip):
        """Everyone a trip put ashore, limited to this vessel's manifest.

        A group booking also carries its fellow passengers in crew_member_ids,
        as HeyPorts IDs typed in by the crew member doing the booking. They are
        neither validated nor checked against a manifest at booking time, so a
        typo — or another ship's ID — would otherwise become a person on this
        report. Resolving them through the manifest drops anything that is not
        this vessel's crew, and an unrecognised ID simply does not count.

        Passengers are resolved to a manifest identity, not to an account, so
        crew who have never registered still count as having gone ashore.
        """
        people = set()
        booker = roster.key_for_profile(trip.crew_id)
        if booker is None and trip.crew_assignment_id:
            assignment = assignments.get(trip.crew_assignment_id)
            if assignment and assignment.hpid:
                booker = roster.key_for_hpid(assignment.hpid)
        if booker:
            people.add(booker)
        extra = trip.crew_member_ids
        if isinstance(extra, list):
            for hpid in extra:
                key = roster.key_for_hpid(hpid)
                if key is not None:
                    people.add(key)
        return people

    # Every departure ashore, as (person, left_at, came_back_at, is_back).
    #
    # "Went ashore" is a count of people, not of paperwork. A stamped out_time
    # proves it, and so does a trip that actually ran: a shore pass approved but
    # never signed out used to leave crew counted as aboard while their cab was
    # running, which is how the report could read "0 went ashore" beside
    # completed trips.
    #
    # is_back is tracked separately from came_back_at because the two answer
    # different questions. A completed cab ride proves the crew member is back
    # aboard even on legacy rows that never recorded the timestamps; those rows
    # can be counted as returned while still contributing no measurable
    # duration. Treating "no end timestamp" as "still ashore" would strand them
    # ashore permanently.
    #
    # A cancelled booking put nobody ashore. Neither did one still waiting for
    # a driver: crew sitting aboard with a pending booking have not left the
    # ship, and counting them as ashore also left them counted as never
    # returning, since there is no trip for them to finish.
    #
    # A cab also carries crew nothing names. `num_passengers` is what the agent
    # booked seats for; `crew_member_ids` is filled in only when the booking
    # crew typed their shipmates' HeyPorts IDs, and most of the fleet has no
    # account to have an ID with. Counting only the people the system can name
    # reported two crew ashore against two four-seat cabs — eight seats — and
    # that undercount is the reported defect, not a data-entry mistake by the
    # agency.
    #
    # So the seats a booking cannot account for by name are counted as people
    # anyway, one anonymous departure each, sharing the trip's own times. They
    # flow through returns, still-ashore and crew-hours identically, which is
    # what keeps "8 went / 8 returned" consistent with the average.
    #
    # This counts journeys, not distinct bodies: four crew taking one cab out
    # and another back are eight seats and read as eight. Nothing in a booking
    # distinguishes that from two cabs of four, so the figure is an upper bound
    # on how many went ashore, exact only when each crew member rides once.
    departures: List[tuple] = []
    for p in passes:
        if p.out_time:
            person = roster.key_for_profile(p.crew_profile_id)
            departures.append((person, p.out_time, p.in_time, p.in_time is not None))
    # Whether a trip's riders are still off the ship is decided by the same
    # per-stop reach events the dashboard tile uses, so the report and the tile
    # cannot disagree in front of the agent. A trip that started but carries no
    # usable reach event is neither ashore nor returned; it is held separately
    # so it inflates neither figure.
    from app.services.crew_ashore import (
        ASHORE, RETURNED, UNKNOWN, _reached_ids_by_booking, booking_ashore_state,
    )

    reached_by_booking = _reached_ids_by_booking(db, day_trips)
    unknown_crew: set = set()
    for t in day_trips:
        if t.status == BookingStatus.CANCELLED:
            continue
        start = t.trip_started_at or t.started_at
        end = t.trip_completed_at or t.completed_at
        state = booking_ashore_state(t, reached_by_booking.get(t.id, ()))
        finished = state != ASHORE
        if not start and state != RETURNED:
            continue
        named = _trip_crew(t)
        riders = list(named) + [
            f"seat:{t.id}:{seat}"
            for seat in range(max(0, (t.num_passengers or 0) - len(named)))
        ]
        for person in riders:
            departures.append((person, start, end, finished))
        if state == UNKNOWN:
            unknown_crew.update(riders)

    ashore_crew = {person for person, _, _, _ in departures if person is not None}
    crew_went_ashore = len(ashore_crew)

    # Still ashore means an unfinished departure — signed out with no sign-in,
    # or a cab that started and has not completed. Reading returns off
    # shore-pass in_time alone was asymmetric with a "went ashore" count that
    # also honours trips: crew who took a cab ashore and back had no pass to
    # sign, so the report showed them gone for good beside their own completed
    # trips, which is the "0 / 1 returned" beside "1 still ashore".
    still_ashore_crew = {
        person for person, _, _, is_back in departures
        if person is not None and not is_back
    }
    # Owner decision: a trip with no reach evidence is not counted as ashore and
    # is not treated as returned either, so it leaves both figures rather than
    # falling into returns by subtraction.
    returned_crew = ashore_crew - still_ashore_crew - unknown_crew

    completed_trips = sum(1 for t in day_trips if t.status == BookingStatus.COMPLETED)

    # Average time ashore, as crew-hours over the crew who actually went.
    #
    #     average = total crew-hours ashore / crew who went ashore
    #
    # Crew-hours count each person's own time, so a cab carrying four people for
    # two hours contributes eight crew-hours, not two. Dividing by the crew who
    # went — not by everyone eligible — answers "how long was a crew member
    # ashore", and does not sag because most of the ship stayed aboard.
    #
    # Crew still ashore have no finished duration to add yet and are left out of
    # both halves; they are reported separately as `still_ashore`.
    #
    # How each person's time is measured. Their departures are merged, then the
    # merged lengths summed. Merging is what stops a cab ride booked *during* a
    # shore pass from being counted twice, since it is not extra time off the
    # ship. Summing the merged pieces — rather than taking the envelope from
    # first departure to last return — is what stops the hours a crew member
    # spent back aboard between two separate trips from being billed as shore
    # leave, which is how four short cab rides came out as "12h 21m".
    spans: Dict[int, List[tuple]] = {}
    for person, start, end, _ in departures:
        if person is None or not start or not end or end < start:
            continue
        spans.setdefault(person, []).append((start, end))

    def _merged_minutes(intervals) -> float:
        total = 0.0
        current_start, current_end = None, None
        for start, end in sorted(intervals):
            if current_end is not None and start <= current_end:
                current_end = max(current_end, end)
                continue
            if current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        if current_end is not None:
            total += (current_end - current_start).total_seconds()
        return total / 60

    person_minutes = [
        _merged_minutes(intervals)
        for person, intervals in spans.items()
        if person not in still_ashore_crew
    ]
    eligible_count = len(eligible_keys)
    average_minutes = (
        sum(person_minutes) / len(person_minutes) if person_minutes else None
    )

    sos_scope = (
        CrewSos.vessel_call_id == call.id
        if strict_call_scope else CrewSos.vessel_id == vessel.id
    )
    day_sos = db.query(CrewSos).filter(
        CrewSos.agency_id == agent_profile.id,
        sos_scope,
        CrewSos.created_at >= day_start,
        CrewSos.created_at < day_end,
    ).all()

    incident_scope = (
        Incident.vessel_call_id == call.id
        if strict_call_scope else Incident.vessel_id == vessel.id
    )
    day_incidents = db.query(Incident).filter(
        Incident.agency_id == agent_profile.id,
        incident_scope,
        Incident.created_at >= day_start,
        Incident.created_at < day_end,
    ).all()


    from app.services import incident_taxonomy as tax

    still_ashore = len(still_ashore_crew)
    resolved_incidents = sum(
        1 for incident in day_incidents
        if incident.status in {IncidentStatus.RESOLVED, IncidentStatus.CANCELLED}
    )
    unresolved_sos = sum(
        1 for sos in day_sos if str(sos.status or "").upper() not in {"CLOSED", "CANCELLED"}
    )
    resolved_sos = len(day_sos) - unresolved_sos

    return ShoreLeaveReportOut(
        vessel_name=call.vessel_name,
        imo_number=call.imo_number,
        berth=vessel.berth_assignment,
        flag=call.flag,
        eta=call.eta,
        etd=call.etd,
        port_name=call.port_name,
        agency_name=call.agency_name,
        agency_logo_url=_agency_logo_display_url(agent_profile),
        report_date=resolved_date,
        period_start=period_start_label,
        period_end=period_end_label,
        generated_at=datetime.utcnow(),
        crew_onboard=len(roster),
        eligible_for_shore_leave=eligible_count,
        # Seats are counted per journey, so a crew that rides more than once can
        # exceed the eligible roster. The printed report should not claim 133%.
        shore_leave_utilisation_pct=(
            min(100, round(crew_went_ashore * 100 / eligible_count))
            if eligible_count else 0
        ),
        crew_went_ashore=crew_went_ashore,
        completed_trips=completed_trips,
        average_duration_minutes=average_minutes,
        returned_safely=len(returned_crew),
        still_ashore=still_ashore,
        sos_raised=len(day_sos),
        incidents_reported=len(day_incidents),
        incidents_resolved=resolved_incidents,
        safety_resolved=resolved_sos + resolved_incidents,
        outstanding_issues=still_ashore + unresolved_sos + (len(day_incidents) - resolved_incidents),
        # Drives the "operation successfully completed" checklist. Anyone still
        # ashore means the day is not closed out, whatever else looks fine.
        all_returned=still_ashore == 0,
        incidents=[
            {
                "incident_id": i.incident_id,
                "category": tax.category_label(i.category),
                "status": i.status.value if hasattr(i.status, "value") else str(i.status),
                "summary": i.title,
            }
            for i in day_incidents
        ],
    )


@router.post("/reports/shore-leave/{vessel_id}/snapshots")
def create_shore_leave_report_snapshot(
    vessel_id: int,
    vessel_call_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Freeze one shore-leave report without changing its source records."""
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can generate report snapshots")

    report = shore_leave_report(
        vessel_id=vessel_id,
        vessel_call_id=vessel_call_id,
        db=db,
        current_user=current_user,
    )
    agent_profile = db.query(AgentProfile).filter(
        AgentProfile.user_id == current_user.id
    ).one()
    resolved_call_id = vessel_call_id or db.query(VesselCall.id).filter(
        VesselCall.vessel_id == vessel_id,
        VesselCall.agency_id == agent_profile.id,
        VesselCall.ended_at.is_(None),
    ).order_by(VesselCall.id.desc()).limit(1).scalar()

    from app.services.report_snapshots import (
        create_report_snapshot,
        serialize_report_snapshot,
    )

    snapshot = create_report_snapshot(
        db,
        report_kind="shore_leave",
        source_id=vessel_id,
        source_reference=(
            f"shore-leave:{vessel_id}:call-{resolved_call_id}:{report.report_date}"
        ),
        agency_id=agent_profile.id,
        vessel_call_id=resolved_call_id,
        generated_by_user_id=current_user.id,
        payload=report,
    )
    db.commit()
    db.refresh(snapshot)
    return serialize_report_snapshot(snapshot)


def _agency_logo_display_url(agent_profile) -> Optional[str]:
    """Where a report should load this agency's logo from.

    Not the stored URL. A report is drawn to a canvas and saved as a PDF, and a
    canvas that has drawn a cross-origin image without CORS headers cannot be
    exported — the logo silently becomes an empty space on the sheet, which is
    what agencies were getting. Object storage sends no such headers unless the
    bucket is configured for it, and that configuration has not happened.

    The API does send them, so pointing the report at our own path makes the
    logo exportable without depending on the bucket at all. Returns None when
    there is no logo, so the caller falls through to its wordmark.
    """
    if agent_profile is None or not (agent_profile.agency_logo_url or "").strip():
        return None
    return f"/api/v1/agents/{agent_profile.id}/logo"


@router.get("/{agency_id}/logo")
def agency_logo(agency_id: int, db: Session = Depends(get_db)):
    """An agency's logo, served with the API's CORS headers.

    Unauthenticated on purpose: these are the same bytes the storage bucket
    already serves publicly to anyone holding the URL, so requiring a token
    here would protect nothing while making the image unusable from an <img>
    tag — which cannot carry an Authorization header, and is what the report
    needs in order to draw the logo into its canvas.
    """
    from fastapi.responses import Response
    from app.services import storage

    profile = db.query(AgentProfile).filter(AgentProfile.id == agency_id).first()
    stored = (profile.agency_logo_url or "").strip() if profile else ""
    if not stored:
        raise HTTPException(status_code=404, detail="No logo for this agency")

    payload = storage.read_bytes(stored)
    if payload is None:
        raise HTTPException(status_code=404, detail="The logo could not be read")
    body, content_type = payload
    return Response(
        content=body,
        media_type=content_type,
        # Logos change rarely and every report download re-fetches this.
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --- crew sign-on and sign-off ----------------------------------------------
#
# HPID specification §8, the agency transfer screen. A joining crew member may
# not be on the sailing manifest and may hold no account, so neither is a
# precondition here. What is required is a passport number, because that is
# what resolves this person to an assignment that may already exist -- from
# the manifest, or from an earlier sign-on -- rather than minting a second
# reference for one person on one call.


class SignOnIn(BaseModel):
    vessel_id: int = Field(gt=0)
    crew_name: str = Field(min_length=1, max_length=255)
    passport_number: str = Field(min_length=1, max_length=64)
    nationality: Optional[str] = None
    rank: Optional[str] = None
    shore_pass_eligible: bool = False


class MovementOut(BaseModel):
    assignment_id: int
    vessel_call_id: int
    reference: Optional[str] = None
    crew_name: str
    source: str
    signed_on_at: Optional[datetime] = None
    signed_off_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # True when this call already held the person, so the agent can see that
    # nothing new was issued rather than wondering why no reference changed.
    reused_existing: bool = False


def _agent_vessel(db: Session, current_user, vessel_id: int):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can do this")
    vessel = (
        db.query(Vessel)
        .filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id)
        .first()
    )
    # Same answer for another agent's vessel as for one that does not exist.
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return vessel


def _movement_out(assignment, *, reused: bool) -> "MovementOut":
    return MovementOut(
        assignment_id=assignment.id,
        vessel_call_id=assignment.vessel_call_id,
        reference=assignment.public_ref,
        crew_name=assignment.crew_name,
        source=assignment.source,
        signed_on_at=assignment.signed_on_at,
        signed_off_at=assignment.signed_off_at,
        ended_at=assignment.ended_at,
        reused_existing=reused,
    )


@router.post("/crew/sign-on", response_model=MovementOut, status_code=201)
def sign_crew_on(
    body: SignOnIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sign a joining crew member on to this vessel's active call."""
    from app.services import crew_movement
    from app.services.historical_context import active_vessel_call

    vessel = _agent_vessel(db, current_user, body.vessel_id)
    before = crew_movement.assignment_on_call_for_passport(
        db,
        vessel_call_id=getattr(
            active_vessel_call(db, vessel, create=False), "id", 0
        ),
        passport_number=body.passport_number,
    )
    try:
        assignment = crew_movement.sign_on(
            db, vessel=vessel, crew_name=body.crew_name,
            passport_number=body.passport_number, nationality=body.nationality,
            rank=body.rank, shore_pass_eligible=body.shore_pass_eligible,
            actor=current_user,
        )
    except crew_movement.MovementError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(assignment)
    return _movement_out(assignment, reused=before is not None)


@router.post("/crew/{assignment_id}/sign-off", response_model=MovementOut)
def sign_crew_off(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """End an assignment, keeping its reference as history (§5.1)."""
    from app.services import crew_movement

    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can do this")
    assignment = (
        db.query(CrewAssignment)
        .join(VesselCall, VesselCall.id == CrewAssignment.vessel_call_id)
        .join(Vessel, Vessel.id == VesselCall.vessel_id)
        .filter(
            CrewAssignment.id == assignment_id,
            Vessel.agent_id == current_user.id,
        )
        .first()
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Crew assignment not found")
    try:
        crew_movement.sign_off(db, assignment=assignment, actor=current_user)
    except crew_movement.MovementError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(assignment)
    return _movement_out(assignment, reused=True)
