from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import cast, String, func
from typing import List, Optional
from datetime import date, datetime, timedelta
import uuid
import json
import urllib.request

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.cab_booking import CabBooking
from app.db.models.cab_pricing import CabPricing
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.notification import Notification
from app.db.models.crew_sos import CrewSos
from app.db.models.port import Port
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.pricing_controls import (
    PricingDuration,
    PricingDurationVisibility,
    PricingProviderSetting,
    PricingRideType,
    PricingRule,
    PricingVehicleCategory,
    PricingVehicleVisibility,
)
from app.api.v1.routes_auth import get_current_user
from app.services.crew_service import generate_hpid
from app.services.booking_service import (
    get_eligible_providers_for_ride,
    vehicle_category_matches,
)
from pydantic import BaseModel, Field

router = APIRouter()
DEFAULT_TRIP_SPEED_KMPH = 28.0


def _fallback_straight_line_distance_km(
    pickup_lat: float,
    pickup_lng: float,
    drop_lat: float,
    drop_lng: float,
) -> float:
    return ((pickup_lat - drop_lat) ** 2 + (pickup_lng - drop_lng) ** 2) ** 0.5 * 111


def _compute_route_distance_km(
    pickup_lat: float,
    pickup_lng: float,
    drop_lat: float,
    drop_lng: float,
) -> float:
    distance_km, _duration_minutes = _compute_route_metrics(
        pickup_lat,
        pickup_lng,
        drop_lat,
        drop_lng,
    )
    return distance_km


def _estimate_minutes_from_distance(distance_km: float) -> float:
    speed = max(5.0, DEFAULT_TRIP_SPEED_KMPH)
    return max(1.0, (max(0.0, distance_km) / speed) * 60.0)


def _compute_route_metrics(
    pickup_lat: float,
    pickup_lng: float,
    drop_lat: float,
    drop_lng: float,
) -> tuple[float, float]:
    # Prefer routed distance over straight line so fare uses realistic road travel.
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{pickup_lng},{pickup_lat};{drop_lng},{drop_lat}"
        "?overview=false&alternatives=false"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OneMarinex/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            routes = payload.get("routes") or []
            first = routes[0] if routes else None
            meters = float((first or {}).get("distance") or 0)
            seconds = float((first or {}).get("duration") or 0)
            if meters > 0:
                distance_km = meters / 1000.0
                if seconds > 0:
                    return distance_km, max(1.0, seconds / 60.0)
                return distance_km, _estimate_minutes_from_distance(distance_km)
    except Exception:
        pass
    fallback_distance = _fallback_straight_line_distance_km(pickup_lat, pickup_lng, drop_lat, drop_lng)
    return fallback_distance, _estimate_minutes_from_distance(fallback_distance)

class ProfileUpdateIn(BaseModel):
    full_name: Optional[str] = None
    rank: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    current_port: Optional[str] = None
    vessel: Optional[str] = None
    data_sharing: Optional[bool] = None
    share_visits: Optional[bool] = None
    safety_tracking: Optional[bool] = None
    communication: Optional[bool] = None
    notifications: Optional[bool] = None
    sos_email: Optional[str] = None


class CrewProfileOut(BaseModel):
    id: int
    user_id: int
    full_name: str
    rank: str
    nationality: str
    passport_number: Optional[str]
    date_of_birth: Optional[date]
    current_port: Optional[str]
    vessel: Optional[str]
    hpid: Optional[str]
    sos_email: Optional[str] = None
    data_sharing: bool
    share_visits: bool
    safety_tracking: bool
    communication: bool
    notifications: bool

    # Synced fields from Vessel and VesselCrew
    vessel_imo: Optional[str] = None
    vessel_type: Optional[str] = None
    berth_assignment: Optional[str] = None
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    vessel_status: Optional[str] = None
    expiry_date: Optional[date] = None
    mapping_status: Optional[str] = None
    shore_pass_eligible: Optional[bool] = None
    agency_name: Optional[str] = None

    class Config:
        from_attributes = True

class ShorePassOut(BaseModel):
    id: int
    agent_name: Optional[str]
    shore_pass_id: str
    hpid: Optional[str]
    port_name: Optional[str]
    vessel_name: Optional[str]
    out_time: Optional[datetime]
    in_time: Optional[datetime]
    expires_at: Optional[datetime]
    is_verified: bool
    status: str
    rejection_reason: Optional[str] = None
    approved_by_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CabBookingCreateIn(BaseModel):
    pickup_address: str
    pickup_lat: float
    pickup_lng: float
    drop_address: str
    drop_lat: float
    drop_lng: float
    vehicle_type: str  # 'ac', 'premium', 'xl'
    vehicle_name: str
    estimated_price: float
    distance_km: float
    num_passengers: int = 1
    port: Optional[str] = None
    crew_member_ids: Optional[List[str]] = None
    scheduled_time: Optional[datetime] = None
    otp: Optional[str] = None
    ride_type: str  # flexible_ride | guaranteed_coordinated_ride

class CabBookingCreateOut(BaseModel):
    booking_id: str
    otp: str
    status: str
    agent_number: str

class CabBookingDetailsOut(BaseModel):
    booking_id: str
    vehicle_name: str
    estimated_price: float
    drop_address: str
    num_passengers: int
    driver_name: Optional[str]
    driver_phone: Optional[str]
    otp: str
    agent_number: str
    helpline_number: Optional[str] = None
    status: str
    ride_type: Optional[str] = None
    ride_type_label: Optional[str] = None
    provider_name: Optional[str] = None
    provider_type: Optional[str] = None
    driver_assigned_at: Optional[datetime] = None
    driver_accepted_at: Optional[datetime] = None
    provider_response_at: Optional[datetime] = None
    trip_started_at: Optional[datetime] = None
    trip_completed_at: Optional[datetime] = None
    distance_km: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class BookingFareUpdateIn(BaseModel):
    estimated_price: float = Field(ge=0)

class CabBookingOut(BaseModel):
    id: int
    booking_id: str
    pickup_address: str
    drop_address: str
    vehicle_type: str
    vehicle_name: str
    estimated_price: float
    num_passengers: int
    status: str
    scheduled_time: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

class CabEstimate(BaseModel):
    vehicle_type: str
    name: str
    estimated_price: float
    distance_km: float
    base_fare: float
    per_km_rate: float


# ─── rich per-vehicle pricing record used by /cab/options ────────────────────
class CabVehiclePricing(BaseModel):
    vehicle_code: str
    vehicle_name: str
    seating_capacity: int
    icon_url: Optional[str]
    description: Optional[str]
    # calculated estimate for this request
    estimated_price: float
    distance_km: float
    # base fare components
    base_fare: float
    minimum_fare: Optional[float]
    # per-unit charges
    price_per_km: Optional[float]
    price_per_minute: Optional[float]
    # waiting / cancellation extras
    free_waiting_minutes: Optional[float]
    extra_waiting_charge_per_min: Optional[float]
    cancellation_fee: Optional[float]
    # package-only extras (null for coordinated_transfer)
    included_km: Optional[float]
    price_per_extra_km: Optional[float]
    price_per_extra_minute: Optional[float]
    price_per_extra_stop: Optional[float]
    # commercial
    platform_commission_pct: Optional[float]
    # dynamic adjustments attached to this rule
    adjustments: List[dict]
    # ride type to pass to /cab/book
    ride_type: str


class CabOptionsResponse(BaseModel):
    port_id: Optional[int]
    port_name: Optional[str]
    distance_km: float
    flexible_cabs: List[CabVehiclePricing]
    aggregator_cabs: List[CabVehiclePricing]


def resolve_port_for_pricing(db: Session, port_value: Optional[str]) -> Optional[Port]:
    if not port_value:
        return None
    normalized = port_value.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return db.query(Port).filter(Port.id == int(normalized)).first()
    return (
        db.query(Port)
        .filter((Port.name.ilike(normalized)) | (Port.code.ilike(normalized)))
        .first()
    )


def map_dynamic_vehicle_type(vehicle_type: str, vehicle_name: str, passenger_count: int) -> str:
    normalized = (vehicle_type or "").strip().lower()
    if normalized in {"ac", "premium", "xl"}:
        return normalized
    label = f"{normalized} {(vehicle_name or '').lower()}"
    if any(token in label for token in ["van", "traveller", "tempo", "premium suv", "premium_suv", "xl"]):
        return "xl"
    if passenger_count > 4 or "suv" in label:
        return "xl"
    if any(token in label for token in ["bike", "auto", "sedan", "cab", "mini"]):
        return "ac"
    return "premium"


def get_dynamic_cab_estimates(
    db: Session,
    distance: float,
    port_value: Optional[str],
    estimate_minutes: Optional[float] = None,
) -> List[CabEstimate]:
    port = resolve_port_for_pricing(db, port_value)
    if not port:
        return []

    ride_type = (
        db.query(PricingRideType)
        .filter(PricingRideType.code == "coordinated_transfer")
        .first()
    )
    if not ride_type:
        return []

    rules = (
        db.query(PricingRule, PricingVehicleCategory)
        .join(PricingVehicleCategory, PricingVehicleCategory.id == PricingRule.vehicle_category_id)
        .filter(
            PricingRule.port_id == port.id,
            PricingRule.ride_type_id == ride_type.id,
            PricingRule.is_active.is_(True),
            PricingRule.is_archived.is_(False),
            PricingRule.duration_id.is_(None),
            PricingVehicleCategory.is_active.is_(True),
        )
        .all()
    )

    cheapest_by_vehicle: dict[int, CabEstimate] = {}
    applied_minutes = float(estimate_minutes if estimate_minutes is not None else _estimate_minutes_from_distance(distance))
    for rule, vehicle in rules:
        subtotal = (
            (rule.base_fare or 0)
            + (distance * (rule.price_per_km or 0))
            + (applied_minutes * (rule.price_per_minute or 0))
        )
        subtotal = max(subtotal, rule.minimum_fare or 0)
        adjustment_multiplier = 1.0
        for adjustment in rule.adjustments or []:
            if adjustment.get("is_active", True) and "multiplier" in adjustment.get("code", ""):
                adjustment_multiplier *= float(adjustment.get("value", 1.0))
        candidate = CabEstimate(
            vehicle_type=vehicle.code,
            name=vehicle.name,
            estimated_price=round(subtotal * adjustment_multiplier, 2),
            distance_km=round(distance, 2),
            base_fare=float(rule.base_fare or 0),
            per_km_rate=float(rule.price_per_km or 0),
        )
        existing = cheapest_by_vehicle.get(vehicle.id)
        if not existing or candidate.estimated_price < existing.estimated_price:
            cheapest_by_vehicle[vehicle.id] = candidate

    return sorted(cheapest_by_vehicle.values(), key=lambda item: item.estimated_price)


def filter_estimates_for_ride_type(
    db: Session,
    estimates: List[CabEstimate],
    ride_type_value: Optional[str],
    port_value: Optional[str],
) -> List[CabEstimate]:
    if not ride_type_value:
        return estimates

    from app.db.models.cab_booking import RideType
    from app.services.booking_service import find_provider_for_ride

    try:
        ride_type = RideType(ride_type_value)
    except ValueError:
        return []

    available_estimates: List[CabEstimate] = []
    for estimate in estimates:
        resolved_vehicle_type = map_dynamic_vehicle_type(
            estimate.vehicle_type,
            estimate.name,
            1,
        )
        try:
            find_provider_for_ride(
                db,
                ride_type,
                port_value,
                resolved_vehicle_type,
                estimate.name,
            )
        except HTTPException:
            continue
        available_estimates.append(estimate)
    return available_estimates

def sync_crew_manifest_helper(profile: CrewProfile, db: Session):
    """
    Tries to match CrewProfile with VesselCrew manifest.
    If match found, updates VesselCrew status to Mapped, syncs vessel name,
    port (if agent has one assigned), and generates an automated ShorePass if not present.
    """
    from app.db.models.vessel_crew import VesselCrew
    from app.db.models.vessel import Vessel
    
    # 1. Try to find VesselCrew matching by generated HPID or passport search
    hpid = profile.hpid or generate_hpid(profile.passport_number, profile.nationality, profile.current_port)
    
    v_crew = db.query(VesselCrew).filter(VesselCrew.hp_id == hpid).first()
    if not v_crew and profile.passport_number:
        # Fallback: search for VesselCrew by passport code in hp_id
        v_crew = db.query(VesselCrew).filter(VesselCrew.hp_id.like(f"HP-{profile.passport_number}-%")).first()
        
    if v_crew:
        # 2. Sync fields
        v_crew.status = "Mapped"
        
        # Find vessel
        vessel = db.query(Vessel).filter(Vessel.id == v_crew.vessel_id).first()
        if vessel:
            # Only sync vessel name from manifest if crew hasn't set one yet
            if not profile.vessel:
                profile.vessel = vessel.name
            
            # Sync port from agent if available and crew doesn't have one set yet
            vessel_port = None
            if vessel.agent and vessel.agent.agent_profile:
                vessel_port = vessel.agent.agent_profile.assigned_port
                
            if vessel_port and not profile.current_port:
                profile.current_port = vessel_port
                
            # Keep HPID aligned
            new_hpid = generate_hpid(profile.passport_number, profile.nationality, profile.current_port)
            profile.hpid = new_hpid
            v_crew.hp_id = new_hpid
            
            # 3. Auto-generate ShorePass if not exists
            port_to_use = vessel_port or profile.current_port or "GEN"
            existing_pass = db.query(ShorePass).filter(
                ShorePass.crew_profile_id == profile.id,
                ShorePass.port_name == port_to_use,
                ShorePass.vessel_name == vessel.name
            ).first()
            
            if not existing_pass:
                port_code = port_to_use.replace("port_", "")[:3].upper()
                vessel_code = vessel.name.replace(" ", "")[:3].upper()
                random_suffix = uuid.uuid4().hex[:4].upper()
                shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"
                
                port_display = port_to_use.replace("port_", "").replace("_", " ").title()
                agent_name = f"{port_display} Port Authority"
                
                new_pass = ShorePass(
                    crew_profile_id=profile.id,
                    agent_name=agent_name,
                    shore_pass_id=shore_pass_id,
                    port_name=port_to_use,
                    vessel_name=vessel.name,
                    is_verified=False,
                    status="pending"
                )
                db.add(new_pass)
                
        try:
            db.commit()
            db.refresh(profile)
            db.refresh(v_crew)
        except Exception as e:
            db.rollback()
            print(f"Error syncing manifest: {e}")

@router.patch("/profile", response_model=dict)
def update_crew_profile(
    body: ProfileUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(
            status_code=403, 
            detail=f"Only crew can update crew profile. Your role: '{current_user.role}'"
        )
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    # Partial update: only update if field is present in request
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)
        
    # Regenerate hpid if port or nationality changed
    if "current_port" in update_data or "nationality" in update_data or "passport_number" in update_data:
        profile.hpid = generate_hpid(profile.passport_number, profile.nationality, profile.current_port)

    # Always generate a unique HPID if passport_number or current_port or nationality is updated
    if (
        'passport_number' in update_data or
        'current_port' in update_data or
        'nationality' in update_data
    ):
        # Use generate_hpid utility
        hpid_candidate = generate_hpid(
            profile.passport_number,
            profile.nationality,
            profile.current_port
        )
        # If passport_number is missing, append user_id to ensure uniqueness
        if not profile.passport_number or profile.passport_number.strip() == "":
            hpid_candidate = f"{hpid_candidate}-{profile.user_id}"
        # Check for uniqueness in DB
        existing = db.query(CrewProfile).filter(CrewProfile.hpid == hpid_candidate, CrewProfile.id != profile.id).first()
        if existing:
            # Append random suffix if still not unique
            import uuid
            hpid_candidate = f"{hpid_candidate}-{uuid.uuid4().hex[:4]}"
        profile.hpid = hpid_candidate

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return {"message": "Profile updated successfully"}

@router.get("/profile", response_model=CrewProfileOut)
def get_crew_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
        
    # Sync with manifest
    sync_crew_manifest_helper(profile, db)
    
    # Expose the extra fields
    from app.db.models.vessel_crew import VesselCrew
    from app.db.models.vessel import Vessel
    
    v_crew = db.query(VesselCrew).filter(VesselCrew.hp_id == profile.hpid).first()
    vessel = None
    if v_crew:
        vessel = db.query(Vessel).filter(Vessel.id == v_crew.vessel_id).first()
        
    profile.vessel_imo = vessel.imo_number if vessel else None
    profile.vessel_type = vessel.vessel_type if vessel else None
    profile.berth_assignment = vessel.berth_assignment if vessel else None
    profile.eta = vessel.eta if vessel else None
    profile.etd = vessel.etd if vessel else None
    profile.vessel_status = vessel.status if vessel else None
    profile.expiry_date = v_crew.expiry_date if v_crew else None
    profile.mapping_status = v_crew.status if v_crew else "Unmapped"
    profile.shore_pass_eligible = v_crew.shore_pass_eligible if v_crew else False
    
    agency_name = None
    if vessel:
        from app.db.models.agent_profile import AgentProfile
        agent_prof = db.query(AgentProfile).filter(AgentProfile.user_id == vessel.agent_id).first()
        if agent_prof:
            agency_name = agent_prof.agency_name
    profile.agency_name = agency_name
    
    return profile

class SOSConfigIn(BaseModel):
    sos_email: str

class SOSTriggerIn(BaseModel):
    port_name: str
    lat: Optional[float] = None
    lng: Optional[float] = None

class SosActiveOut(BaseModel):
    active: bool
    id: Optional[int] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    port_name: Optional[str] = None
    vessel: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class SosCancelOut(BaseModel):
    status: str
    message: str

class FeedbackIn(BaseModel):
    message: str

@router.post("/sos-config", response_model=dict)
def update_sos_config(
    body: SOSConfigIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can update SOS config")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
        
    profile.sos_email = body.sos_email
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
    return {"message": "SOS config updated successfully", "sos_email": profile.sos_email}

@router.get("/sos/active", response_model=SosActiveOut)
def get_active_sos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view SOS status")

    active = db.query(CrewSos).filter(
        CrewSos.user_id == current_user.id,
        CrewSos.status.in_(["ACTIVE", "ACKNOWLEDGED"]),
    ).order_by(CrewSos.created_at.desc()).first()

    if not active:
        return {"active": False}

    return {
        "active": True,
        "id": active.id,
        "status": active.status,
        "created_at": active.created_at,
        "port_name": active.port_name,
        "vessel": active.vessel,
        "lat": active.lat,
        "lng": active.lng,
    }

@router.post("/sos/cancel", response_model=SosCancelOut)
def cancel_active_sos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can cancel SOS")

    active = db.query(CrewSos).filter(
        CrewSos.user_id == current_user.id,
        CrewSos.status.in_(["ACTIVE", "ACKNOWLEDGED"]),
    ).order_by(CrewSos.created_at.desc()).first()

    if not active:
        return {"status": "inactive", "message": "No active SOS request"}

    active.status = "CANCELLED"
    active.cancelled_at = datetime.utcnow()
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "cancelled", "message": "SOS request cancelled"}

@router.post("/trigger-sos")
def trigger_sos(
    body: SOSTriggerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Trigger an SOS alert.
    Sends notification to:
    1. Ship's configured SOS email
    2. HeyPorts support
    """
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can trigger SOS")
        
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    active = db.query(CrewSos).filter(
        CrewSos.user_id == current_user.id,
        CrewSos.status == "ACTIVE",
    ).order_by(CrewSos.created_at.desc()).first()
    if active:
        raise HTTPException(status_code=409, detail="An active SOS request already exists")
    
    if not profile.sos_email:
        raise HTTPException(status_code=400, detail="SOS Email not configured")

    port_name = body.port_name or profile.current_port

    # 1. Ship Email
    recipients = [profile.sos_email]

    # 2. HeyPorts Support
    recipients.append("support@heyports.com")
    
    # In a real app, send emails here
    print(f"[SOS TRIGGERED] From: {current_user.email}, Port: {body.port_name}, Lat: {body.lat}, Lng: {body.lng}")
    print(f"[SOS RECIPIENTS] {', '.join(set(recipients))}")
    
    # Record SOS request
    new_sos = CrewSos(
        user_id=current_user.id,
        crew_profile_id=profile.id,
        port_name=port_name,
        vessel=profile.vessel,
        lat=body.lat,
        lng=body.lng,
        status="ACTIVE",
    )
    db.add(new_sos)
    db.flush()

    location_text = "this location"
    if body.lat is not None and body.lng is not None:
        location_text = f"this location ({body.lat}, {body.lng})"

    sos_notification = Notification(
        title="SOS Alert",
        message=(
            f"Crew member {profile.full_name} raised SOS in {location_text}. "
            "If you are nearby please get in touch."
        ),
        port_name=port_name or None,
        vessel=profile.vessel or None,
        created_by=current_user.id,
        sos_id=new_sos.id,
    )
    db.add(sos_notification)

    # Also record as an incident (for Super Admin tracking)
    incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
    description = (
        f"SOS triggered by {profile.full_name} (Vessel: {profile.vessel or 'N/A'}) "
        f"at {port_name}. Location: {body.lat}, {body.lng}"
    )
    new_incident = Incident(
        incident_id=incident_id,
        type=IncidentType.CREW,
        title="SOS Alert",
        description=description,
        status=IncidentStatus.ACTIVE,
        port_name=port_name,
        reporter_name=profile.full_name or current_user.name,
        reporter_role=profile.rank,
        reporter_id=profile.hpid or profile.passport_number,
    )
    db.add(new_incident)
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to record SOS: {str(e)}")
        
    return {
        "status": "success",
        "message": "SOS Alert sent to all recipients",
        "recipients_count": len(set(recipients)),
        "incident_id": incident_id,
    }

@router.post("/feedback")
def submit_feedback(
    body: FeedbackIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Store user feedback and log it.
    """
    print(f"[FEEDBACK] From: {current_user.email}, Message: {body.message}")
    
    # Optionally store in DB
    from app.db.models.incident import Incident
    incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
    crew = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    feedback_incident = Incident(
        incident_id=incident_id,
        type=IncidentType.CREW,
        title="Crew Feedback",
        description=body.message,
        status=IncidentStatus.ACTIVE,
        reporter_name=current_user.name,
        reporter_role=crew.rank if crew else "Crew",
        reporter_id=crew.hpid or crew.passport_number if crew else None,
        port_name=crew.current_port if crew else None,
    )
    db.add(feedback_incident)
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        # Non-critical failure
        
    return {"status": "success", "message": "Feedback received"}

class GenerateShorePassIn(BaseModel):
    port_name: Optional[str] = None
    vessel_name: Optional[str] = None

@router.post("/generate-shorepass", response_model=ShorePassOut)
def generate_shorepass(
    body: GenerateShorePassIn = GenerateShorePassIn(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "crew":
        raise HTTPException(
            status_code=403, 
            detail=f"Only crew can generate shore pass. Your role: '{current_user.role}'"
        )
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    # Use explicitly passed port/vessel, fallback to profile values
    port = body.port_name or profile.current_port
    vessel = body.vessel_name or profile.vessel

    if not port or not vessel:
        raise HTTPException(status_code=400, detail="Port and Vessel must be selected first")

    # Derive agent name from port (e.g. "port_singapore" -> "Singapore Port Authority")
    port_display = port.replace("port_", "").replace("_", " ").title()
    agent_name = f"{port_display} Port Authority"

    # Build unique shore pass ID: port code + vessel code + random
    port_code = port.replace("port_", "")[:3].upper()          # e.g. "SIN"
    vessel_code = vessel.replace("vessel_", "V")[:3].upper()   # e.g. "V1"
    random_suffix = uuid.uuid4().hex[:4].upper()
    shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"

    # Update HPID in profile based on current port and Passport Number
    profile.hpid = generate_hpid(profile.passport_number, profile.nationality, port)

    # Generate shore pass
    new_pass = ShorePass(
        crew_profile_id=profile.id,
        agent_name=agent_name,
        shore_pass_id=shore_pass_id,
        port_name=port,
        vessel_name=vessel,
        out_time=None,
        in_time=None,
        expires_at=None,
        is_verified=False,
        status="pending"
    )
    
    db.add(new_pass)
    try:
        db.commit()
        db.refresh(new_pass)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    return new_pass

@router.get("/shorepass", response_model=Optional[ShorePassOut])
def get_current_shorepass(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        return None
    
    # Get the latest shore pass for the CURRENT port
    last_pass = db.query(ShorePass).filter(
        ShorePass.crew_profile_id == profile.id,
        ShorePass.port_name == profile.current_port
    ).order_by(ShorePass.created_at.desc()).first()
    return last_pass

@router.get("/shorepass/eligibility")
def check_shorepass_eligibility(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check if the crew member's vessel is managed by any agent.
    Returns under_agent=true only if the vessel name AND the crew's HPID
    are both found in some agent's vessel_crew mapping.
    """
    from app.db.models.vessel import Vessel
    from app.db.models.vessel_crew import VesselCrew
    from app.db.models.user import User as UserModel

    if current_user.role != "crew":
        return {"under_agent": False, "agent_name": None}

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile or not profile.vessel or not profile.hpid:
        return {"under_agent": False, "agent_name": None}

    # Check if this vessel name exists AND has a vessel_crew entry with matching hp_id
    matching_vessel = (
        db.query(Vessel)
        .join(VesselCrew, VesselCrew.vessel_id == Vessel.id)
        .filter(
            Vessel.name.ilike(f"%{profile.vessel}%"),
            VesselCrew.hp_id == profile.hpid,
        )
        .first()
    )

    if not matching_vessel:
        return {"under_agent": False, "agent_name": None}

    # Get the agent's name
    agent_user = db.query(UserModel).filter(UserModel.id == matching_vessel.agent_id).first()
    return {
        "under_agent": True,
        "agent_name": agent_user.name if agent_user else None,
    }

@router.post("/shorepass/{pass_id}/verify", response_model=ShorePassOut)
def verify_shorepass(
    pass_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    shore_pass = db.query(ShorePass).filter(
        ShorePass.id == pass_id,
        ShorePass.crew_profile_id == profile.id
    ).first()
    
    if not shore_pass:
        raise HTTPException(status_code=404, detail="Shore pass not found")
    
    # Auto-match / Verify logic
    shore_pass.status = "approved"
    shore_pass.is_verified = True
    
    # In a real scenario, we might look up the agent who added the crew
    # For now, we'll set a default name if it's auto-matched
    if not shore_pass.approved_by_name:
        shore_pass.approved_by_name = "Vikram Patel" # Default as per screenshot
    
    # Set default times if agent didn't set them (2 days duration as a placeholder)
    if not shore_pass.out_time:
        shore_pass.out_time = datetime.now()
    if not shore_pass.expires_at:
        shore_pass.expires_at = datetime.now() + timedelta(days=2)
    if not shore_pass.in_time:
        shore_pass.in_time = shore_pass.expires_at

    try:
        db.commit()
        db.refresh(shore_pass)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    return shore_pass

@router.get("/shorepass/history", response_model=List[ShorePassOut])
def get_shorepass_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all shore passes for the current user (newest first)"""
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        return []
    
    passes = db.query(ShorePass).filter(
        ShorePass.crew_profile_id == profile.id
    ).order_by(ShorePass.created_at.desc()).all()
    return passes

# ─── Provider-type bridge ─────────────────────────────────────────────────────
# pricing_controls tables use plural keys (partner_drivers / aggregators).
# AggregatorProfile.provider_type and booking_service use singular keys
# (partnered_driver / aggregator).
# This mapping bridges the two domains so availability checks use the right key.
_PRICING_TYPE_TO_BOOKING: dict[str, str] = {
    "partner_drivers": "partnered_driver",
    "aggregators": "aggregator",
}
# Reverse: booking provider_type → pricing rule provider_type
_BOOKING_TYPE_TO_PRICING: dict[str, str] = {v: k for k, v in _PRICING_TYPE_TO_BOOKING.items()}


def _has_active_provider_for_port(
    db: Session,
    port_id: int,
    booking_provider_type: str,
) -> bool:
    """Return True if at least one Active AggregatorProfile with active drivers
    exists for the given port and provider type."""
    from app.db.models.aggregator_profile import AggregatorProfile
    from sqlalchemy.orm import joinedload as _jl

    providers = (
        db.query(AggregatorProfile)
        .options(_jl(AggregatorProfile.drivers))
        .filter(
            AggregatorProfile.operating_port_id == port_id,
            AggregatorProfile.provider_type == booking_provider_type,
            AggregatorProfile.status == "Active",
        )
        .all()
    )
    return any(
        any((d.status or "").lower() != "offline" for d in (p.drivers or []))
        for p in providers
    )


def _vehicle_has_provider(
    db: Session,
    port_id: int,
    booking_provider_type: str,
    vehicle_code: str,
    vehicle_name: str,
) -> bool:
    """Return True if at least one active driver at this port and provider type
    carries a matching vehicle."""
    from app.db.models.aggregator_profile import AggregatorProfile
    from sqlalchemy.orm import joinedload as _jl

    providers = (
        db.query(AggregatorProfile)
        .options(_jl(AggregatorProfile.drivers))
        .filter(
            AggregatorProfile.operating_port_id == port_id,
            AggregatorProfile.provider_type == booking_provider_type,
            AggregatorProfile.status == "Active",
        )
        .all()
    )
    for provider in providers:
        for driver in provider.drivers or []:
            if (driver.status or "").lower() == "offline":
                continue
            if vehicle_category_matches(db, port_id, vehicle_code, vehicle_name, driver.vehicle_type):
                return True
    return False


def _build_cab_options_for_provider(
    db: Session,
    port_id: int,
    ride_type_obj: "PricingRideType",
    pricing_provider_type: str,   # key used in PricingRule.provider_type  e.g. "partner_drivers"
    booking_ride_type: str,        # value to put in CabVehiclePricing.ride_type e.g. "flexible_ride"
    distance_km: float,
    duration_minutes: Optional[int] = None,
    estimate_minutes: Optional[float] = None,
) -> List[CabVehiclePricing]:
    """
    Return one CabVehiclePricing per vehicle for the given provider type and port.

    Steps:
    1. Resolve the booking-system provider_type from the pricing provider_type.
    2. Quick check: if no active provider of this type exists at the port, return [].
    3. Pull all active, non-archived PricingRules for (port, ride_type, provider_type).
    4. For each vehicle category keep the cheapest rule.
    5. Filter out vehicles for which no active driver at this port can serve
       (provider has matching vehicle type on any active driver).
    """
    booking_provider_type = _PRICING_TYPE_TO_BOOKING.get(pricing_provider_type, pricing_provider_type)
    is_package_trip = (getattr(ride_type_obj, "code", "") or "") == "package_trip"

    provider_setting = (
        db.query(PricingProviderSetting)
        .filter(
            PricingProviderSetting.port_id == port_id,
            PricingProviderSetting.ride_type_id == ride_type_obj.id,
            PricingProviderSetting.provider_type == pricing_provider_type,
        )
        .first()
    )
    if provider_setting is not None:
        if not bool(provider_setting.is_active):
            return []
        # Superadmin minimum duration guard for this provider type.
        if duration_minutes and provider_setting.minimum_bookable_hours:
            min_minutes = int(round(float(provider_setting.minimum_bookable_hours) * 60))
            if duration_minutes < min_minutes:
                return []
        cfg = provider_setting.config or {}
        if isinstance(cfg, dict):
            allow_package = cfg.get("allow_package_trips")
            if allow_package is False:
                return []

    # For coordinated transfers we require an active serving provider; package cards
    # should still be shown from superadmin pricing settings even before driver assignment.
    if not is_package_trip and not _has_active_provider_for_port(db, port_id, booking_provider_type):
        return []

    selected_duration: Optional[PricingDuration] = None
    duration_is_visible = True
    vehicle_visibility_by_id: dict[int, bool] = {}
    duration_minutes_value = duration_minutes if duration_minutes and duration_minutes > 0 else None
    if bool(getattr(ride_type_obj, "supports_duration", False)):
        if duration_minutes_value is None:
            return []
        durations = (
            db.query(PricingDuration)
            .filter(
                PricingDuration.port_id == port_id,
                PricingDuration.ride_type_id == ride_type_obj.id,
                PricingDuration.is_active.is_(True),
            )
            .all()
        )
        if not durations:
            return []
        selected_duration = min(
            durations,
            key=lambda d: abs((d.duration_minutes or 0) - duration_minutes_value),
        )

        duration_visibility = (
            db.query(PricingDurationVisibility)
            .filter(
                PricingDurationVisibility.port_id == port_id,
                PricingDurationVisibility.ride_type_id == ride_type_obj.id,
                PricingDurationVisibility.provider_type == pricing_provider_type,
                PricingDurationVisibility.duration_id == selected_duration.id,
            )
            .first()
        )
        # If a row exists, honor it. If no row exists, default to visible for
        # backward compatibility on ports that have not configured visibility yet.
        if duration_visibility is not None:
            duration_is_visible = bool(duration_visibility.is_visible)
        if not duration_is_visible:
            return []

        vehicle_visibility_rows = (
            db.query(PricingVehicleVisibility)
            .filter(
                PricingVehicleVisibility.port_id == port_id,
                PricingVehicleVisibility.ride_type_id == ride_type_obj.id,
                PricingVehicleVisibility.provider_type == pricing_provider_type,
                PricingVehicleVisibility.duration_id == selected_duration.id,
            )
            .all()
        )
        vehicle_visibility_by_id = {
            row.vehicle_category_id: bool(row.is_visible)
            for row in vehicle_visibility_rows
        }

    rules_query = (
        db.query(PricingRule, PricingVehicleCategory)
        .join(PricingVehicleCategory, PricingVehicleCategory.id == PricingRule.vehicle_category_id)
        .filter(
            PricingRule.port_id == port_id,
            PricingRule.ride_type_id == ride_type_obj.id,
            PricingRule.provider_type == pricing_provider_type,
            PricingRule.is_active.is_(True),
            PricingRule.is_archived.is_(False),
            PricingVehicleCategory.is_active.is_(True),
        )
    )
    if selected_duration:
        rules = rules_query.filter(PricingRule.duration_id == selected_duration.id).all()
        # Backward compatibility: if this duration has no explicit rules,
        # fall back to generic (duration_id is NULL) rules.
        if not rules:
            rules = rules_query.filter(PricingRule.duration_id.is_(None)).all()
    else:
        rules = rules_query.filter(PricingRule.duration_id.is_(None)).all()

    def _apply_platform_commission(amount: float, pct: Optional[float]) -> float:
        if pct is None:
            return amount
        try:
            commission_pct = float(pct)
        except (TypeError, ValueError):
            return amount
        if commission_pct <= 0:
            return amount
        return amount * (1.0 + (commission_pct / 100.0))

    # Keep cheapest rule per vehicle category
    best: dict[int, tuple] = {}
    for rule, vehicle in rules:
        applied_minutes = float(estimate_minutes if estimate_minutes is not None else (duration_minutes_value or 0))
        if (ride_type_obj.pricing_mode or "").lower() == "package":
            subtotal = float(rule.base_fare or 0)
            if rule.included_km is not None and rule.price_per_extra_km:
                extra_km = max(0.0, distance_km - float(rule.included_km or 0))
                subtotal += extra_km * float(rule.price_per_extra_km or 0)
            elif rule.price_per_km:
                # Backward-compatible fallback for ports that still use per-km package rules.
                subtotal += distance_km * float(rule.price_per_km or 0)

            if selected_duration and rule.price_per_extra_minute:
                extra_minutes = max(0.0, applied_minutes - float(selected_duration.duration_minutes or 0))
                subtotal += extra_minutes * float(rule.price_per_extra_minute or 0)
            elif rule.price_per_minute:
                # Backward-compatible fallback when package extra-minute config is not set.
                subtotal += applied_minutes * float(rule.price_per_minute or 0)
        else:
            subtotal = (
                float(rule.base_fare or 0)
                + (distance_km * float(rule.price_per_km or 0))
                + (applied_minutes * float(rule.price_per_minute or 0))
            )
            if rule.included_km is not None and rule.price_per_extra_km:
                extra_km = max(0.0, distance_km - float(rule.included_km or 0))
                subtotal += extra_km * float(rule.price_per_extra_km or 0)
            if selected_duration and rule.price_per_extra_minute:
                extra_minutes = max(0.0, applied_minutes - float(selected_duration.duration_minutes or 0))
                subtotal += extra_minutes * float(rule.price_per_extra_minute or 0)
        subtotal = max(subtotal, rule.minimum_fare or 0)
        multiplier = 1.0
        for adj in rule.adjustments or []:
            if adj.get("is_active", True) and "multiplier" in adj.get("code", ""):
                multiplier *= float(adj.get("value", 1.0))
        final = round(_apply_platform_commission(subtotal * multiplier, rule.platform_commission_pct), 2)
        existing = best.get(vehicle.id)
        if existing is None or final < existing[0]:
            best[vehicle.id] = (final, rule, vehicle)

    result = []
    for final_price, rule, vehicle in sorted(best.values(), key=lambda x: x[0]):
        if selected_duration and vehicle_visibility_by_id:
            if vehicle_visibility_by_id.get(vehicle.id) is False:
                continue
        # Per-vehicle availability: only include if a matching driver exists at this port
        if not is_package_trip and not _vehicle_has_provider(db, port_id, booking_provider_type, vehicle.code, vehicle.name):
            continue
        result.append(
            CabVehiclePricing(
                vehicle_code=vehicle.code,
                vehicle_name=vehicle.name,
                seating_capacity=vehicle.seating_capacity,
                icon_url=vehicle.icon_url,
                description=vehicle.description,
                estimated_price=final_price,
                distance_km=round(distance_km, 2),
                base_fare=float(rule.base_fare or 0),
                minimum_fare=rule.minimum_fare,
                price_per_km=rule.price_per_km,
                price_per_minute=rule.price_per_minute,
                free_waiting_minutes=rule.free_waiting_minutes,
                extra_waiting_charge_per_min=rule.extra_waiting_charge,
                cancellation_fee=rule.cancellation_fee,
                included_km=rule.included_km,
                price_per_extra_km=rule.price_per_extra_km,
                price_per_extra_minute=rule.price_per_extra_minute,
                price_per_extra_stop=rule.price_per_extra_stop,
                platform_commission_pct=rule.platform_commission_pct,
                adjustments=rule.adjustments or [],
                ride_type=booking_ride_type,
            )
        )
    return result


def _resolve_server_side_fare(
    db: Session,
    *,
    pickup_lat: float,
    pickup_lng: float,
    drop_lat: float,
    drop_lng: float,
    port_value: Optional[str],
    booking_ride_type: str,
    vehicle_type: str,
    vehicle_name: str,
) -> tuple[Optional[float], float]:
    distance_km, estimated_minutes = _compute_route_metrics(pickup_lat, pickup_lng, drop_lat, drop_lng)
    resolved_port = resolve_port_for_pricing(db, port_value)
    if not resolved_port:
        return None, round(distance_km, 2)

    ride_type_obj = (
        db.query(PricingRideType)
        .filter(PricingRideType.code == "coordinated_transfer")
        .first()
    )
    if not ride_type_obj:
        return None, round(distance_km, 2)

    pricing_provider_type = _BOOKING_TYPE_TO_PRICING.get(booking_ride_type)
    if not pricing_provider_type:
        return None, round(distance_km, 2)

    options = _build_cab_options_for_provider(
        db,
        port_id=resolved_port.id,
        ride_type_obj=ride_type_obj,
        pricing_provider_type=pricing_provider_type,
        booking_ride_type=booking_ride_type,
        distance_km=distance_km,
        estimate_minutes=estimated_minutes,
    )
    if not options:
        return None, round(distance_km, 2)

    resolved_vehicle = map_dynamic_vehicle_type(vehicle_type, vehicle_name, 1)
    exact = next(
        (
            option
            for option in options
            if (option.vehicle_code or "").lower() == resolved_vehicle
        ),
        None,
    )
    if exact:
        return float(exact.estimated_price), round(distance_km, 2)

    name_match = next(
        (
            option
            for option in options
            if (option.vehicle_name or "").strip().lower() == (vehicle_name or "").strip().lower()
        ),
        None,
    )
    if name_match:
        return float(name_match.estimated_price), round(distance_km, 2)

    return float(options[0].estimated_price), round(distance_km, 2)


@router.get("/cab/options", response_model=CabOptionsResponse)
def get_cab_options(
    pickup_lat: Optional[float] = None,
    pickup_lng: Optional[float] = None,
    drop_lat: Optional[float] = None,
    drop_lng: Optional[float] = None,
    port: Optional[str] = None,
    ride_type_code: str = "coordinated_transfer",
    duration_hours: Optional[float] = None,
    distance_km_override: Optional[float] = None,
    num_passengers: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    Returns all cabs available for a port split by provider type.

    - flexible_cabs    → HeyPorts partnered drivers  (ride_type = "flexible_ride")
    - aggregator_cabs  → Fleet aggregators            (ride_type = "guaranteed_coordinated_ride")

    A cab appears only when:
      1. An active pricing rule exists for this port + provider type + vehicle.
      2. An active provider of the matching type operates at this port.
      3. That provider has at least one active driver carrying a matching vehicle type.

    Each entry carries the full pricing breakdown so the UI can display fare
    estimates without an additional call. Pass `vehicle_code` as `vehicle_type`
    and the `ride_type` value directly to POST /api/v1/crew/cab/book.

    Port accepts: port name (e.g. "Mumbai Port"), port code, or numeric port ID.
    """
    if distance_km_override is not None and distance_km_override >= 0:
        distance_km = float(distance_km_override)
        route_minutes = _estimate_minutes_from_distance(distance_km)
    else:
        if None in (pickup_lat, pickup_lng, drop_lat, drop_lng):
            raise HTTPException(status_code=400, detail="pickup/drop coordinates or distance_km_override are required")
        distance_km, route_minutes = _compute_route_metrics(
            float(pickup_lat),
            float(pickup_lng),
            float(drop_lat),
            float(drop_lng),
        )

    duration_minutes = int(round(duration_hours * 60)) if duration_hours and duration_hours > 0 else None
    effective_estimate_minutes = float(duration_minutes if duration_minutes is not None else route_minutes)

    resolved_port = resolve_port_for_pricing(db, port)
    if not resolved_port:
        return CabOptionsResponse(
            port_id=None,
            port_name=None,
            distance_km=round(distance_km, 2),
            flexible_cabs=[],
            aggregator_cabs=[],
        )

    ride_type_obj = (
        db.query(PricingRideType)
        .filter(PricingRideType.code == ride_type_code)
        .first()
    )
    if not ride_type_obj:
        return CabOptionsResponse(
            port_id=resolved_port.id,
            port_name=resolved_port.name,
            distance_km=round(distance_km, 2),
            flexible_cabs=[],
            aggregator_cabs=[],
        )

    from app.db.models.cab_booking import RideType as BookingRideType

    flexible_cabs = _build_cab_options_for_provider(
        db,
        port_id=resolved_port.id,
        ride_type_obj=ride_type_obj,
        pricing_provider_type="partner_drivers",
        booking_ride_type=BookingRideType.FLEXIBLE_RIDE.value,
        distance_km=distance_km,
        duration_minutes=duration_minutes,
        estimate_minutes=effective_estimate_minutes,
    )
    aggregator_cabs = _build_cab_options_for_provider(
        db,
        port_id=resolved_port.id,
        ride_type_obj=ride_type_obj,
        pricing_provider_type="aggregators",
        booking_ride_type=BookingRideType.GUARANTEED_COORDINATED_RIDE.value,
        distance_km=distance_km,
        duration_minutes=duration_minutes,
        estimate_minutes=effective_estimate_minutes,
    )

    if num_passengers and num_passengers > 0:
        flexible_cabs = [cab for cab in flexible_cabs if (cab.seating_capacity or 0) >= num_passengers]
        aggregator_cabs = [cab for cab in aggregator_cabs if (cab.seating_capacity or 0) >= num_passengers]

    return CabOptionsResponse(
        port_id=resolved_port.id,
        port_name=resolved_port.name,
        distance_km=round(distance_km, 2),
        flexible_cabs=flexible_cabs,
        aggregator_cabs=aggregator_cabs,
    )


@router.get("/cab/ride-availability")
def get_ride_availability(
    port: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can check ride availability")
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    port_value = port or (profile.current_port if profile else None)
    from app.services.booking_service import get_ride_availability as compute_availability
    return compute_availability(db, port_value)


@router.post("/cab/book", response_model=CabBookingCreateOut)
def book_cab(
    body: CabBookingCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    from app.db.models.cab_booking import VehicleType, BookingStatus, RideType
    from app.db.models.booking_timeline import TimelineEventType
    from app.services.booking_service import is_ride_type_available
    from app.services.timeline_service import create_timeline_event

    try:
        ride_type = RideType(body.ride_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ride type")

    port_value = body.port or profile.current_port
    resolved_vehicle_type = map_dynamic_vehicle_type(
        body.vehicle_type,
        body.vehicle_name,
        body.num_passengers,
    )

    if not is_ride_type_available(
        db,
        ride_type,
        port_value,
        resolved_vehicle_type,
        body.vehicle_name,
    ):
        raise HTTPException(status_code=400, detail="Selected ride type is not available for this port")

    broadcast_providers = get_eligible_providers_for_ride(
        db,
        ride_type,
        port_value,
        resolved_vehicle_type,
        body.vehicle_name,
    )
    if not broadcast_providers:
        raise HTTPException(status_code=400, detail="No eligible providers available for this ride type and port")

    resolved_price, resolved_distance = _resolve_server_side_fare(
        db,
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        drop_lat=body.drop_lat,
        drop_lng=body.drop_lng,
        port_value=port_value,
        booking_ride_type=ride_type.value,
        vehicle_type=resolved_vehicle_type,
        vehicle_name=body.vehicle_name,
    )
    final_price = resolved_price if resolved_price is not None else body.estimated_price
    final_distance = resolved_distance if resolved_distance > 0 else body.distance_km

    booking_id = f"CAB-{uuid.uuid4().hex[:8].upper()}"
    otp = body.otp or (profile.ride_otp if profile else None) or "1234"
    now = datetime.utcnow()

    new_booking = CabBooking(
        booking_id=booking_id,
        crew_id=profile.id,
        pickup_address=body.pickup_address,
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        drop_address=body.drop_address,
        drop_lat=body.drop_lat,
        drop_lng=body.drop_lng,
        vehicle_type=VehicleType(resolved_vehicle_type),
        vehicle_name=body.vehicle_name,
        vehicle_category=body.vehicle_name,
        estimated_price=final_price,
        distance_km=final_distance,
        num_passengers=body.num_passengers,
        port=port_value,
        crew_member_ids=body.crew_member_ids,
        scheduled_time=body.scheduled_time,
        otp=otp,
        ride_type=ride_type,
        provider_id=None,
        aggregator_id=None,
        aggregator_name=None,
        agent_number="+91 9876543251",
        helpline_number="+91 1800-HEYPORTS",
        status=BookingStatus.PENDING_PROVIDER_RESPONSE,
    )

    db.add(new_booking)
    db.flush()

    create_timeline_event(
        db,
        booking_db_id=new_booking.id,
        event_type=TimelineEventType.BOOKING_CREATED,
        actor_id=profile.id,
        actor_type="crew",
        metadata={"ride_type": ride_type.value},
        event_time=now,
    )
    create_timeline_event(
        db,
        booking_db_id=new_booking.id,
        event_type=TimelineEventType.PROVIDER_NOTIFIED,
        actor_id=None,
        actor_type="system",
        metadata={
            "eligible_provider_count": len(broadcast_providers),
            "eligible_provider_ids": [provider.id for provider in broadcast_providers],
        },
        event_time=now,
    )

    try:
        db.commit()
        db.refresh(new_booking)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return CabBookingCreateOut(
        booking_id=new_booking.booking_id,
        otp=new_booking.otp,
        status=new_booking.status.value,
        agent_number=new_booking.agent_number
    )

# Duplicate route removed/commented out
# @router.get("/cab/history", response_model=List[CabBookingOut])
# def get_cab_history(
#     db: Session = Depends(get_db),
#     current_user: User = Depends(get_current_user)
# ):
#     profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
#     if not profile:
#         return []
#     
#     history = db.query(CabBooking).filter(CabBooking.crew_id == profile.id).order_by(CabBooking.created_at.desc()).all()
#     return history

@router.get("/cab/estimate", response_model=List[CabEstimate])
def get_cab_estimates(
    pickup_lat: float,
    pickup_lng: float,
    drop_lat: float,
    drop_lng: float,
    port: Optional[str] = None,
    ride_type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    distance, route_minutes = _compute_route_metrics(pickup_lat, pickup_lng, drop_lat, drop_lng)

    dynamic_estimates = get_dynamic_cab_estimates(db, distance, port, estimate_minutes=route_minutes)
    if dynamic_estimates:
        return filter_estimates_for_ride_type(db, dynamic_estimates, ride_type, port)
    
    pricings = db.query(CabPricing).all()
    
    # If table is empty, return default pricing
    if not pricings:
        # Seed values for initial run if empty
        default_pricings = [
            {"type": "ac", "name": "Cab AC", "base": 50, "rate": 15, "min": 100},
            {"type": "premium", "name": "Cab Premium AC", "base": 80, "rate": 22, "min": 180},
            {"type": "xl", "name": "Cab XL AC", "base": 120, "rate": 30, "min": 250},
        ]
        res = []
        for dp in default_pricings:
            est_price = dp["base"] + (distance * dp["rate"])
            final_price = max(est_price, dp["min"])
            res.append(CabEstimate(
                vehicle_type=dp["type"],
                name=dp["name"],
                estimated_price=round(final_price, 2),
                distance_km=round(distance, 2),
                base_fare=float(dp["base"]),
                per_km_rate=float(dp["rate"])
            ))
        return filter_estimates_for_ride_type(db, res, ride_type, port)

    estimates = []
    for p in pricings:
        est_price = p.base_fare + (distance * p.per_km_rate)
        final_price = max(est_price, p.minimum_fare)
        estimates.append(CabEstimate(
            vehicle_type=p.vehicle_type,
            name=p.vehicle_type, # Or add a 'name' field to model if needed
            estimated_price=round(final_price, 2),
            distance_km=round(distance, 2),
            base_fare=p.base_fare,
            per_km_rate=p.per_km_rate
        ))
    return filter_estimates_for_ride_type(db, estimates, ride_type, port)

@router.get("/cab/bookings/{booking_id}", response_model=CabBookingDetailsOut)
def get_booking_details(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get detailed information about a specific booking"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view bookings")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    booking = db.query(CabBooking).filter(
        CabBooking.booking_id == booking_id,
        CabBooking.crew_id == profile.id
    ).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    from app.services.booking_service import RIDE_TYPE_LABELS, serialize_booking
    serialized = serialize_booking(booking)
    return CabBookingDetailsOut(
        booking_id=booking.booking_id,
        vehicle_name=booking.vehicle_name,
        estimated_price=float(booking.estimated_price),
        drop_address=booking.drop_address,
        num_passengers=booking.num_passengers,
        driver_name=serialized.get("driver_name") or "Not Yet Assigned",
        driver_phone=serialized.get("driver_phone") or "Not Yet Assigned",
        otp=booking.otp,
        agent_number=booking.agent_number,
        helpline_number=serialized.get("helpline_number"),
        status=booking.status.value,
        ride_type=serialized.get("ride_type"),
        ride_type_label=serialized.get("ride_type_label"),
        provider_name=serialized.get("provider_name"),
        provider_type=serialized.get("provider_type"),
        driver_assigned_at=serialized.get("driver_assigned_at"),
        driver_accepted_at=serialized.get("driver_accepted_at"),
        provider_response_at=serialized.get("provider_response_at"),
        trip_started_at=serialized.get("trip_started_at"),
        trip_completed_at=serialized.get("trip_completed_at"),
        distance_km=float(booking.distance_km or 0),
        created_at=booking.created_at
    )


@router.patch("/cab/bookings/{booking_id}/fare")
def update_booking_fare(
    booking_id: str,
    body: BookingFareUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update the final fare for a crew booking."""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can update booking fare")

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    booking = db.query(CabBooking).filter(
        CabBooking.booking_id == booking_id,
        CabBooking.crew_id == profile.id,
    ).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking.estimated_price = round(float(body.estimated_price), 2)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update fare: {str(exc)}")

    return {"booking_id": booking.booking_id, "estimated_price": float(booking.estimated_price)}

@router.put("/cab/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cancel a cab booking"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can cancel bookings")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    booking = db.query(CabBooking).filter(
        CabBooking.booking_id == booking_id,
        CabBooking.crew_id == profile.id
    ).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    from app.db.models.cab_booking import BookingStatus
    if booking.status == BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Booking already cancelled")
    
    if booking.status == BookingStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Cannot cancel completed booking")
    
    from app.db.models.booking_timeline import TimelineEventType
    from app.services.timeline_service import create_timeline_event

    if booking.status in {
        BookingStatus.ON_TRIP,
        BookingStatus.COMPLETED,
        BookingStatus.PROVIDER_REJECTED,
    }:
        raise HTTPException(status_code=400, detail="Cannot cancel booking in current status")

    booking.status = BookingStatus.CANCELLED
    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.TRIP_CANCELLED,
        actor_id=profile.id,
        actor_type="crew",
        metadata={"reason": "cancelled_by_crew"},
    )

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to cancel booking: {str(e)}")

    return {"message": "Booking cancelled successfully", "booking_id": booking_id}

@router.get("/cab/history", response_model=List[CabBookingOut])
def get_booking_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all cab bookings for the current user"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view booking history")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    bookings = (
        db.query(
            CabBooking.id,
            CabBooking.booking_id,
            CabBooking.pickup_address,
            CabBooking.drop_address,
            cast(CabBooking.vehicle_type, String).label("vehicle_type"),
            CabBooking.vehicle_name,
            CabBooking.estimated_price,
            CabBooking.num_passengers,
            cast(CabBooking.status, String).label("status"),
            CabBooking.scheduled_time,
            CabBooking.created_at,
        )
        .filter(CabBooking.crew_id == profile.id)
        .order_by(CabBooking.created_at.desc())
        .all()
    )
    
    return [
        CabBookingOut(
            id=booking.id,
            booking_id=booking.booking_id,
            pickup_address=booking.pickup_address,
            drop_address=booking.drop_address,
            vehicle_type=(booking.vehicle_type or "").lower(),
            vehicle_name=booking.vehicle_name,
            estimated_price=float(booking.estimated_price),
            num_passengers=booking.num_passengers,
            status=(booking.status or "").lower(),
            scheduled_time=booking.scheduled_time,
            created_at=booking.created_at
        )
        for booking in bookings
    ]
