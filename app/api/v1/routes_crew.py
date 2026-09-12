from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import cast, String, func, or_, text
from sqlalchemy.exc import IntegrityError
from typing import List, Optional
from datetime import date, datetime, timedelta
import hashlib
import logging
import re
import secrets
import uuid
import json
import urllib.request

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.cab_booking import CabBooking
from app.db.models.cab_pricing import CabPricing
from app.db.models.driver import Driver
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.notification import Notification
from app.db.models.crew_sos import CrewSos, CrewSosTimelineEvent
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.port import Port
from app.db.models.port_rule import PortRule
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.agent_profile import AgentProfile
from app.db.models.booking_invitation import BookingInvitation
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
from app.services.crew_service import generate_hpid, ensure_stable_hpid
from app.services.crew_reference import normalize_nationality, normalize_rank
from app.services.booking_service import (
    get_eligible_providers_for_ride,
    vehicle_category_matches,
    STATUS_LABELS,
)
from app.services.port_time import (
    as_port_local,
    minutes_from_hhmm as _minutes_from_hhmm,
    port_clock_snapshot,
    port_closing_buffer_reason,
    port_closed_reason as _port_closed_reason,
)
from pydantic import BaseModel, EmailStr, Field

router = APIRouter()
logger = logging.getLogger(__name__)
DEFAULT_TRIP_SPEED_KMPH = 28.0
PACKAGE_CLOSING_BUFFER_MINUTES = 2 * 60
# Which way a coordinated transfer runs. Port opening hours gate trips that
# leave the port; a return leg ends at the gate, so crew already ashore must
# still be able to book one — most of all once closing time has passed.
TRANSFER_DIRECTIONS = {"to_city", "return_to_port"}
DEFAULT_TRANSFER_DIRECTION = "to_city"


def _assignment_call_or_conflict(assignment: CrewAssignment):
    call = assignment.vessel_call
    if call is None:
        raise HTTPException(
            status_code=409,
            detail="The selected vessel assignment has no vessel call context",
        )
    return call


def _port_rule_for(db: Session, port_value: Optional[str]) -> Optional[PortRule]:
    """PortRule for a port referred to by code, name, or the raw stored value.

    Bookings carry whichever of those the client happened to send, so match on
    all three rather than assuming one.
    """
    if not port_value:
        return None
    port = (
        db.query(Port)
        .filter((Port.code == port_value) | (Port.name == port_value))
        .first()
    )
    candidates = [
        c for c in ([port.code, port.name, port_value] if port else [port_value]) if c
    ]
    if not candidates:
        return None
    return db.query(PortRule).filter(PortRule.port_name.in_(candidates)).first()


def _planned_return_is_after_closing(
    pickup_at: datetime,
    planned_return: str,
    closing_time: str,
) -> bool:
    pickup_minutes = pickup_at.hour * 60 + pickup_at.minute
    return_minutes = _minutes_from_hhmm(planned_return)
    closing_minutes = _minutes_from_hhmm(closing_time)

    # A time before a late-evening pickup belongs to the following day. This
    # supports ports whose configured closing time is after midnight.
    is_late_pickup = pickup_minutes >= 12 * 60
    if is_late_pickup and return_minutes < pickup_minutes and return_minutes < 12 * 60:
        return_minutes += 24 * 60
    if is_late_pickup and closing_minutes < pickup_minutes and closing_minutes < 12 * 60:
        closing_minutes += 24 * 60

    return return_minutes > closing_minutes


def _planned_return_is_before_pickup(
    pickup_at: datetime,
    planned_return: str,
    closing_time: Optional[str] = None,
) -> bool:
    pickup_minutes = pickup_at.hour * 60 + pickup_at.minute
    return_minutes = _minutes_from_hhmm(planned_return)

    # A return earlier on the clock is next-day only when a late pickup's port
    # closing schedule actually crosses midnight. Otherwise it is an invalid
    # same-day return before pickup.
    if closing_time:
        closing_minutes = _minutes_from_hhmm(closing_time)
        closing_crosses_midnight = (
            pickup_minutes >= 12 * 60
            and closing_minutes < pickup_minutes
            and closing_minutes < 12 * 60
        )
        if (
            closing_crosses_midnight
            and return_minutes < pickup_minutes
            and return_minutes < 12 * 60
        ):
            return_minutes += 24 * 60

    return return_minutes < pickup_minutes


def _extract_package_duration_label(vehicle_name: str) -> str:
    """Package bookings encode duration into the free-text vehicle_name
    (e.g. "Sedan 3h Package (Partner)") since there's no dedicated duration
    column on CabBooking today. Best-effort extraction for WhatsApp copy."""
    match = re.search(r"(\d+)\s*h\s*Package", vehicle_name or "")
    return f"{match.group(1)} hours" if match else "N/A"


def _compute_shore_pass_expiry(db: Session, port_name: Optional[str], out_time: datetime) -> datetime:
    """Resolve a shore pass's real expiry from the port's configured
    closing_time, anchored to out_time's date, rolling to the next day if
    that closing time already passed today (same midnight-crossing heuristic
    as heyports-frontend's isShoreLeaveValid() in CoordinatedTransfer.tsx).
    Falls back to the prior +2 day placeholder if no PortRule/closing_time
    is configured for this port."""
    fallback = out_time + timedelta(days=2)
    if not port_name:
        return fallback

    port = (
        db.query(Port)
        .filter((Port.code == port_name) | (Port.name == port_name))
        .first()
    )
    candidates = [c for c in ([port.code, port.name, port_name] if port else [port_name]) if c]
    rule = db.query(PortRule).filter(PortRule.port_name.in_(candidates)).first()
    if not rule or not rule.closing_time:
        return fallback

    try:
        hour, minute = (int(x) for x in rule.closing_time.split(":")[:2])
    except (ValueError, AttributeError):
        return fallback

    expiry = out_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if expiry < out_time and hour < 12 and out_time.hour >= 12:
        expiry = expiry + timedelta(days=1)
    return expiry


def is_partnered_agency(agency_name: Optional[str]) -> bool:
    if not agency_name:
        return False
    clean = agency_name.strip().lower()
    return clean not in ["other", "others", "none", "n/a", "", "other agency"]


def _unmanaged_vessel_call(db: Session, profile):
    """The vessel and call for crew on a ship no agency runs.

    Shore leave is an agency's to grant, so a vessel recorded as "Other" has no
    agent, no manifest and no crew assignments — and never will. Everything that
    waits for an assignment therefore waits forever, which is why cabs stayed
    locked behind "your agent has not uploaded the crew list" on a vessel that
    has no agent to do it.

    Returns the vessel the crew member selected and its open call, but only when
    that vessel really is unmanaged. A partnered vessel missing an assignment is
    a crew member their agent has not added yet, which is a genuine wait and
    still refused.
    """
    from app.db.models.vessel import Vessel
    from app.services.historical_context import active_vessel_call

    if profile is None or not profile.selected_vessel_id:
        return None, None
    vessel = db.query(Vessel).filter(Vessel.id == profile.selected_vessel_id).first()
    if vessel is None:
        return None, None

    agency = vessel.agency_name
    if not agency and vessel.agent and getattr(vessel.agent, "agent_profile", None):
        agency = vessel.agent.agent_profile.agency_name
    if is_partnered_agency(agency):
        return None, None

    return vessel, active_vessel_call(db, vessel, create=False)


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
    # The vessel actually chosen, by id. The name beside it is a label; this is
    # what the shore leave card resolves against.
    selected_vessel_id: Optional[int] = None
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
    has_partnered_agency: bool = False
    vessel_exists: bool = False

    # Which shore leave card to show, decided here rather than reassembled from
    # several fields by every screen that needs it.
    #
    #   APPROVED      an assignment exists and the agent marked them eligible
    #   NOT_ELIGIBLE  an assignment exists and the agent marked them not
    #   PENDING       no assignment yet, on a vessel an agency manages — the
    #                 agent has not uploaded the manifest. Waiting, not allowed
    #   None          no assignment, on a vessel no agency runs here; there is
    #                 no shore leave to have a status about
    #
    # PENDING is emphatically not an assignment. Bookings and shore passes stay
    # refused until one exists; the card only says who they are waiting for.
    shore_leave_status: Optional[str] = None
    # Cab booking needs an agency to arrange it, so it is locked in exactly the
    # two states that have one but no permission yet.
    cab_booking_locked: bool = False

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
    planned_return: Optional[str] = None
    ride_type: str  # flexible_ride | guaranteed_coordinated_ride
    trip_type: Optional[str] = None  # package_trip | coordinated_transfer
    direction: Optional[str] = None  # to_city | return_to_port
    crew_assignment_id: Optional[int] = Field(default=None, gt=0)
    # Temporarily optional for a backend-first deployment. New clients always
    # send and reuse it; legacy callers receive no retry guarantee.
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=64)

class CabBookingCreateOut(BaseModel):
    booking_id: str
    otp: str
    status: str
    agent_number: Optional[str] = None


class EligibleCrewAssignmentOut(BaseModel):
    crew_assignment_id: int
    vessel_call_id: int
    vessel_id: Optional[int] = None
    vessel_name: str
    imo_number: Optional[str] = None
    agency_id: Optional[int] = None
    agency_name: Optional[str] = None
    port_id: Optional[int] = None
    port_code: Optional[str] = None
    port_name: Optional[str] = None
    started_at: datetime
    emergency_email: Optional[str] = None


@router.get("/assignments/eligible")
def list_eligible_crew_assignments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can list vessel assignments")
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    from app.services.historical_context import eligible_assignments_for_profile

    assignments = [
        row
        for row in eligible_assignments_for_profile(db, profile)
        if row.vessel_call is not None
        and row.vessel_call.vessel_id is not None
        and bool(row.vessel_call.vessel_name)
    ]
    # Whether an assignment is something this crew member is waiting for.
    #
    # On a vessel no agency runs there is no agent, no manifest and no
    # assignment coming, so an empty list is the settled answer rather than a
    # wait — and a screen that says "your agent has not uploaded the crew list"
    # is naming someone who does not exist. The caller needs to tell the two
    # apart, and only the server knows which vessel was selected.
    _unmanaged_vessel, _unmanaged_call = _unmanaged_vessel_call(db, profile)
    return {
        "agency_managed": _unmanaged_vessel is None,
        "assignments": [
            EligibleCrewAssignmentOut(
                crew_assignment_id=row.id,
                vessel_call_id=row.vessel_call_id,
                vessel_id=row.vessel_call.vessel_id,
                vessel_name=row.vessel_call.vessel_name,
                imo_number=row.vessel_call.imo_number,
                agency_id=row.vessel_call.agency_id,
                agency_name=row.vessel_call.agency_name,
                port_id=row.vessel_call.port_id,
                port_code=(row.vessel_call.port.code if row.vessel_call.port else None),
                port_name=row.vessel_call.port_name,
                started_at=row.started_at,
                emergency_email=row.emergency_email,
            ).model_dump()
            for row in assignments
        ],
        "requires_selection": len(assignments) > 1,
    }

class CabBookingDetailsOut(BaseModel):
    booking_id: str
    vehicle_name: str
    estimated_price: float
    drop_address: str
    num_passengers: int
    driver_name: Optional[str]
    driver_phone: Optional[str]
    assigned_driver_id: Optional[int] = None
    otp: str
    agent_number: Optional[str] = None
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
    is_owner: bool = True
    itinerary_stops: Optional[list] = None

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
    trip_started_at: Optional[datetime] = None
    created_at: datetime
    is_owner: bool = True

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


def _return_drop_address(db: Session, port_value: Optional[str]) -> Optional[str]:
    """Where a return leg ends, or None when the port cannot be resolved.

    The booking screen shows this and prices against it, so it must be the
    same string the booking is stored with — never a client-side guess.
    """
    resolved_port = resolve_port_for_pricing(db, port_value)
    port_label = (resolved_port.name if resolved_port else port_value or "").strip()
    return f"{port_label} Main Gate" if port_label else None


def _canonical_return_drop_address(db: Session, port_value: Optional[str]) -> str:
    drop_address = _return_drop_address(db, port_value)
    if not drop_address:
        raise HTTPException(
            status_code=400,
            detail="A current port is required for a return-to-port booking",
        )
    return drop_address


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
    """Materialise every exact manifest membership for ``profile``.

    Registration cannot choose one vessel when the same person is legitimately
    present on two manifests.  This helper therefore maps every exact identity
    match and never rewrites the profile's display vessel/port.
    """
    from app.db.models.vessel_crew import VesselCrew
    from app.db.models.vessel import Vessel
    
    stable_hpid = ensure_stable_hpid(db, profile)
    # An HPID uniquely issued to this signed-in profile is safe. Passport-only
    # matches are accepted only when their name and nationality also agree;
    # production contains reused passport values, so an OR query over passport
    # alone would let one account claim several different people.
    from app.services.crew_identity import (
        normalize_passport_number,
        normalized_passport_expression,
        normalized_person_name,
    )

    hpid_manifests = db.query(VesselCrew).filter(
        func.upper(func.trim(VesselCrew.hp_id)) == stable_hpid.upper()
    ).all()
    passport = normalize_passport_number(profile.passport_number)
    passport_manifests = (
        db.query(VesselCrew)
        .filter(normalized_passport_expression(VesselCrew.passport_number) == passport)
        .all()
        if passport
        else []
    )
    profile_name = normalized_person_name(profile.full_name)
    profile_nationality = normalize_nationality(profile.nationality, strict=False)
    manifests_by_id = {row.id: row for row in hpid_manifests}
    for row in passport_manifests:
        row_nationality = normalize_nationality(row.nationality, strict=False)
        # Matched on the passport alone. Requiring the name to match too is
        # what kept crew off their own vessel: the agent had them on the list,
        # the passport agreed, and a different spelling of the same person left
        # the row unmapped, so no assignment was created and no shore pass card
        # ever appeared.
        if not row_nationality or row_nationality == profile_nationality:
            manifests_by_id[row.id] = row
    manifests = list(manifests_by_id.values())

    for v_crew in manifests:
        v_crew.status = "Mapped"
        vessel = db.query(Vessel).filter(Vessel.id == v_crew.vessel_id).first()
        if vessel:
            vessel_port = None
            if vessel.agent and vessel.agent.agent_profile:
                vessel_port = vessel.agent.agent_profile.assigned_port
            v_crew.hp_id = stable_hpid
            from app.services.historical_context import assignment_for_manifest

            assignment = assignment_for_manifest(db, vessel, v_crew, profile=profile)
            if assignment and assignment.crew_profile_id is None:
                profile_collision = db.query(CrewAssignment.id).filter(
                    CrewAssignment.vessel_call_id == assignment.vessel_call_id,
                    CrewAssignment.crew_profile_id == profile.id,
                    CrewAssignment.ended_at.is_(None),
                    CrewAssignment.id != assignment.id,
                ).first()
                if profile_collision is None:
                    assignment.crew_profile_id = profile.id
                else:
                    logger.warning(
                        "Skipped duplicate active assignment link for profile %s on call %s",
                        profile.id,
                        assignment.vessel_call_id,
                    )

            agency_name = vessel.agency_name
            if not agency_name and vessel.agent and hasattr(vessel.agent, "agent_profile") and vessel.agent.agent_profile:
                agency_name = vessel.agent.agent_profile.agency_name

            if is_partnered_agency(agency_name) and assignment:
                port_to_use = assignment.vessel_call.port_name or vessel_port or "GEN"
                existing_pass = db.query(ShorePass).filter(
                    ShorePass.crew_profile_id == profile.id,
                    ShorePass.crew_assignment_id == assignment.id,
                ).first()
                if not existing_pass:
                    port_code = port_to_use.replace("port_", "")[:3].upper()
                    vessel_code = vessel.name.replace(" ", "")[:3].upper()
                    random_suffix = uuid.uuid4().hex[:8].upper()
                    shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"
                    
                    port_display = port_to_use.replace("port_", "").replace("_", " ").title()
                    agent_name = f"{port_display} Port Authority"
                    
                    new_pass = ShorePass(
                        crew_profile_id=profile.id,
                        crew_assignment_id=assignment.id,
                        vessel_call_id=assignment.vessel_call_id,
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
    except IntegrityError:
        db.rollback()
        logger.exception("Unable to materialise manifest assignments for crew %s", profile.id)
        raise
    except Exception:
        db.rollback()
        logger.exception("Unable to materialise manifest assignments for crew %s", profile.id)
        raise

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
    if "passport_number" in update_data and update_data["passport_number"] is not None:
        # Registration refuses a passport that identifies nobody, and one that
        # already belongs to someone. Editing had neither check, so the same
        # value could simply be typed in here afterwards — which is how one
        # account ended up holding another person's passport.
        from app.services.crew_identity import (
            CrewIdentityConflict,
            passport_already_registered,
            validate_passport_number,
        )

        raw = update_data["passport_number"]
        if str(raw).strip():
            try:
                passport = validate_passport_number(raw)
            except CrewIdentityConflict as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            owner = passport_already_registered(
                db, passport, exclude_profile_id=profile.id)
            if owner is not None:
                owner_user = db.query(User).filter(User.id == owner.user_id).first()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This passport number is already linked to "
                        f"{owner_user.email if owner_user else 'another account'}. "
                        f"Please sign in with those credentials."
                    ),
                )
            update_data["passport_number"] = passport
    if "nationality" in update_data:
        try:
            update_data["nationality"] = normalize_nationality(update_data["nationality"], strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "rank" in update_data:
        update_data["rank"] = normalize_rank(update_data["rank"])
    for field, value in update_data.items():
        setattr(profile, field, value)
        
    # HPID is intentionally not regenerated when mutable profile fields change.
    ensure_stable_hpid(db, profile)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return {"message": "Profile updated successfully"}

@router.get("/profile", response_model=CrewProfileOut)
def get_crew_profile(
    crew_assignment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
        
    from app.services.historical_context import selected_assignment_for_profile

    try:
        assignment = selected_assignment_for_profile(
            db, profile, crew_assignment_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    output = CrewProfileOut.model_validate(profile)
    if assignment is None:
        # No assignment, so the card turns on the vessel they chose. An agency
        # runs it and has not added them yet — PENDING, and waiting on someone
        # — or no agency runs it here, in which case shore leave is not a thing
        # they are waiting for and no card belongs on the screen.
        #
        # Resolved from the selected vessel id rather than the profile's vessel
        # *name*: the list spans every port now, and two ships can share a name.
        from app.db.models.vessel import Vessel

        selected = (
            db.query(Vessel).filter(Vessel.id == profile.selected_vessel_id).first()
            if profile.selected_vessel_id else None
        )
        selected_agency = None
        if selected is not None:
            selected_agency = selected.agency_name
            if not selected_agency and selected.agent and getattr(
                selected.agent, "agent_profile", None
            ):
                selected_agency = selected.agent.agent_profile.agency_name
        agency_managed = is_partnered_agency(selected_agency)
        return output.model_copy(update={
            "mapping_status": "Unmapped",
            "shore_pass_eligible": False,
            "agency_name": selected_agency or "Other",
            "has_partnered_agency": agency_managed,
            "vessel_exists": selected is not None,
            "shore_leave_status": "PENDING" if agency_managed else None,
            # Waiting on an agent is still not permission to book a cab.
            "cab_booking_locked": agency_managed,
        })

    call = _assignment_call_or_conflict(assignment)
    vessel = call.vessel
    manifest = assignment.vessel_crew
    agency_name = call.agency_name or "Other"
    return output.model_copy(update={
        "sos_email": assignment.emergency_email,
        "current_port": call.port_name,
        "vessel": call.vessel_name,
        "vessel_imo": call.imo_number,
        "vessel_type": vessel.vessel_type if vessel else None,
        "berth_assignment": vessel.berth_assignment if vessel else None,
        "eta": call.eta,
        "etd": call.etd,
        "vessel_status": vessel.status if vessel else call.status,
        "expiry_date": manifest.expiry_date if manifest else None,
        "mapping_status": manifest.status if manifest else "Mapped",
        "shore_pass_eligible": assignment.shore_pass_eligible,
        "agency_name": agency_name,
        "has_partnered_agency": is_partnered_agency(agency_name),
        "vessel_exists": vessel is not None,
        # An assignment exists, so the agent has decided one way or the other.
        "shore_leave_status": (
            "APPROVED" if assignment.shore_pass_eligible else "NOT_ELIGIBLE"
        ),
        "cab_booking_locked": not assignment.shore_pass_eligible,
    })

class SOSConfigIn(BaseModel):
    crew_assignment_id: Optional[int] = Field(default=None, gt=0)
    sos_email: EmailStr

class SOSTriggerIn(BaseModel):
    trip_id: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class SosEligibilityOut(BaseModel):
    eligible: bool
    reason: Optional[str] = None
    trip_id: Optional[str] = None
    email_configured: bool

class SosActiveOut(BaseModel):
    active: bool
    id: Optional[int] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    port_name: Optional[str] = None
    vessel: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    trip_id: Optional[str] = None

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
        
    from app.services.historical_context import selected_assignment_for_profile

    try:
        assignment = selected_assignment_for_profile(
            db, profile, body.crew_assignment_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if assignment is None:
        raise HTTPException(
            status_code=409,
            detail="No active vessel assignment is available for SOS configuration",
        )
    assignment.emergency_email = str(body.sos_email).strip()
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
    return {
        "message": "SOS config updated successfully",
        "sos_email": assignment.emergency_email,
        "crew_assignment_id": assignment.id,
    }


def _active_sos_bookings(db: Session, crew_profile_id: int) -> list[CabBooking]:
    from app.db.models.cab_booking import BookingStatus

    return (
        db.query(CabBooking)
        .filter(
            CabBooking.crew_id == crew_profile_id,
            CabBooking.status.in_([
                BookingStatus.DRIVER_ASSIGNED,
                BookingStatus.DRIVER_ACCEPTED,
                BookingStatus.ON_TRIP,
            ]),
        )
        .order_by(CabBooking.created_at.desc(), CabBooking.id.desc())
        .all()
    )


def _active_sos_booking_for_trip(
    db: Session, crew_profile_id: int, trip_id: str
) -> Optional[CabBooking]:
    requested = (trip_id or "").strip()
    if not requested:
        return None
    return next(
        (
            row
            for row in _active_sos_bookings(db, crew_profile_id)
            if row.booking_id == requested
        ),
        None,
    )


@router.get("/sos/eligibility", response_model=SosEligibilityOut)
def get_sos_eligibility(
    trip_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can check SOS eligibility")
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    active_bookings = _active_sos_bookings(db, profile.id)
    active_booking = (
        _active_sos_booking_for_trip(db, profile.id, trip_id)
        if trip_id
        else (active_bookings[0] if len(active_bookings) == 1 else None)
    )
    if active_booking is None and len(active_bookings) > 1 and not trip_id:
        return {
            "eligible": False,
            "reason": "Select an active trip before using SOS.",
            "trip_id": None,
            # This flag describes the selected operational context. With no
            # selected trip there is intentionally no single SOS address.
            "email_configured": False,
        }

    assignment = active_booking.crew_assignment if active_booking else None
    emergency_email = (
        (assignment.emergency_email or "").strip() if assignment else ""
    )
    email_configured = bool(
        (current_user.email or "").strip() and emergency_email
    )
    reason = None
    if not email_configured:
        reason = "A crew email and ship SOS email are required."
    elif active_booking is None:
        reason = "SOS is available only when you have an active trip."

    return {
        "eligible": reason is None,
        "reason": reason,
        "trip_id": active_booking.booking_id if active_booking else None,
        "email_configured": email_configured,
    }

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
        "trip_id": active.trip_id,
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
        CrewSos.status.in_(["ACTIVE", "ACKNOWLEDGED"]),
    ).order_by(CrewSos.created_at.desc()).first()
    if active:
        raise HTTPException(status_code=409, detail="An active SOS request already exists")

    if not (current_user.email or "").strip():
        raise HTTPException(status_code=400, detail="Crew email is required for SOS")
    # The submitted trip selects the exact assignment. A profile-level email
    # cannot safely identify the ship when this person sails on two vessels.
    requested_trip_id = body.trip_id.strip()
    active_booking = _active_sos_booking_for_trip(
        db, profile.id, requested_trip_id
    )
    if active_booking is None:
        raise HTTPException(
            status_code=400,
            detail="SOS requires an existing active trip belonging to this crew member",
        )
    assignment = active_booking.crew_assignment
    emergency_email = (
        (assignment.emergency_email or "").strip() if assignment else ""
    )
    if not emergency_email:
        raise HTTPException(status_code=400, detail="Ship SOS email is not configured")

    port_name = active_booking.port
    from app.services.historical_context import event_context

    historical = event_context(db, booking=active_booking, profile=profile)
    vessel_snapshot = (
        historical["vessel_call"].vessel_name
        if historical["vessel_call"]
        else None
    )

    # 1. Ship Email
    recipients = [emergency_email]

    # 2. HeyPorts Support
    recipients.append("support@heyports.com")

    # Alert the ship's configured SOS email + HeyPorts support. send_sos_alert
    # never raises — an SMTP outage must not block recording the SOS. Delivers
    # for real once SMTP_* is configured; logs otherwise.
    from app.services.email import send_sos_alert
    send_sos_alert(
        ship_email=emergency_email,
        crew_name=profile.full_name or current_user.email,
        crew_email=current_user.email,
        vessel=vessel_snapshot,
        port_name=port_name,
        lat=body.lat,
        lng=body.lng,
    )

    # Record SOS request
    new_sos = CrewSos(
        user_id=current_user.id,
        crew_profile_id=profile.id,
        cab_booking_id=active_booking.id,
        vessel_call_id=(
            historical["vessel_call"].id if historical["vessel_call"] else None
        ),
        vessel_id=historical["vessel_id"],
        agency_id=historical["agency_id"],
        crew_assignment_id=historical["crew_assignment_id"],
        port_id=historical["port_id"],
        context_resolution=historical["context_resolution"],
        trip_id=active_booking.booking_id,
        crew_email=current_user.email.strip(),
        sos_email=emergency_email,
        port_name=port_name,
        vessel=vessel_snapshot,
        lat=body.lat,
        lng=body.lng,
        status="ACTIVE",
    )
    db.add(new_sos)
    db.flush()
    db.add(CrewSosTimelineEvent(
        sos_id=new_sos.id,
        source="system",
        event_type="TRIGGERED",
        label="SOS triggered by crew",
        detail="Location and active trip were verified by the server.",
        actor_name=profile.full_name,
    ))

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
        vessel=vessel_snapshot or None,
        created_by=current_user.id,
        sos_id=new_sos.id,
    )
    db.add(sos_notification)

    # An SOS is deliberately NOT mirrored into an Incident row.
    #
    # It used to be, "for Super Admin tracking", but superadmins already get the
    # full SOS list from GET /api/v1/sos/admin, and agents get their own from
    # the safety summary — both read CrewSos directly. The mirror only created
    # a second record of one event: the SOS showed up in Incident Management
    # alongside the SOS page, it inflated the open-incident counts, and because
    # nothing linked the two, closing the SOS left its twin sitting open
    # forever. The vessel report feed reads CrewSos too, so the vessel page
    # labelled the event "Incident" instead of "SOS".
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to record SOS: {str(e)}")

    try:
        from app.services.whatsapp import (
            notify_sos_crew_in_danger,
            notify_sos_crew_and_admin,
            notify_sos_aggregator,
        )

        def _fmt_location(lat, lng):
            return f"{lat:.5f}, {lng:.5f}" if lat is not None and lng is not None else "Location unavailable"

        location_str = _fmt_location(body.lat, body.lng)

        # 1. Crew member themselves — always fires
        notify_sos_crew_in_danger(current_user.mobile_number)

        # 2. Historical call owner + all superadmins with a phone on file.
        trip_id_for_admin = active_booking.booking_id
        agent_profile = (
            db.query(AgentProfile)
            .filter(AgentProfile.id == new_sos.agency_id)
            .first()
            if new_sos.agency_id
            else None
        )
        if agent_profile and agent_profile.user:
            notify_sos_crew_and_admin(
                agent_profile.user.mobile_number, trip_id_for_admin,
                profile.full_name, vessel_snapshot or "N/A", location_str,
            )
        superadmins = db.query(User).filter(
            User.role == "superadmin",
            User.mobile_number.isnot(None),
            User.mobile_number != "",
        ).all()
        for admin in superadmins:
            notify_sos_crew_and_admin(
                admin.mobile_number, trip_id_for_admin,
                profile.full_name, vessel_snapshot or "N/A", location_str,
            )

        # 2b. Fellow crew on this exact vessel call. Port-wide matching leaked
        # emergencies to unrelated agencies berthed at the same harbour.
        from app.db.models.crew_assignment import CrewAssignment

        fellow_profile_ids = []
        if new_sos.vessel_call_id:
            fellow_profile_ids = [
                row[0]
                for row in db.query(CrewAssignment.crew_profile_id)
                .filter(
                    CrewAssignment.vessel_call_id == new_sos.vessel_call_id,
                    CrewAssignment.ended_at.is_(None),
                    CrewAssignment.crew_profile_id.isnot(None),
                    CrewAssignment.crew_profile_id != profile.id,
                )
                .all()
            ]
        fellow_crew = (
            db.query(CrewProfile)
            .join(User, User.id == CrewProfile.user_id)
            .filter(
                CrewProfile.id.in_(fellow_profile_ids),
                User.mobile_number.isnot(None),
                User.mobile_number != "",
            )
            .all()
            if fellow_profile_ids
            else []
        )
        for fellow in fellow_crew:
            notify_sos_crew_and_admin(
                fellow.user.mobile_number, trip_id_for_admin,
                profile.full_name, vessel_snapshot or "N/A", location_str,
            )

        # 3. Aggregator on the verified active booking, if any.
        provider_profile = active_booking.provider or active_booking.aggregator
        if provider_profile and provider_profile.user:
            notify_sos_aggregator(
                provider_profile.user.mobile_number,
                active_booking.booking_id, location_str,
                datetime.now().strftime("%I:%M:%S %p"),
                lat=body.lat, lng=body.lng,
            )
    except Exception:
        logger.exception("WhatsApp SOS notify failed for user %s", current_user.id)

    return {
        "status": "success",
        "message": "SOS Alert sent to all recipients",
        "id": new_sos.id,
        "recipients_count": len(set(recipients)),
        "incident_id": None,
        "trip_id": active_booking.booking_id,
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
    from app.services.historical_context import event_context

    historical = event_context(db, profile=crew)
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
        vessel_id=historical["vessel_id"],
        vessel_call_id=(
            historical["vessel_call"].id if historical["vessel_call"] else None
        ),
        agency_id=historical["agency_id"],
        crew_profile_id=crew.id if crew else None,
        crew_assignment_id=historical["crew_assignment_id"],
        port_id=historical["port_id"],
        context_resolution=historical["context_resolution"],
    )
    db.add(feedback_incident)
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        # Non-critical failure
        
    return {"status": "success", "message": "Feedback received"}

class GenerateShorePassIn(BaseModel):
    crew_assignment_id: Optional[int] = Field(default=None, gt=0)
    port_name: Optional[str] = None
    vessel_name: Optional[str] = None

@router.post("/generate-shorepass", response_model=Optional[ShorePassOut])
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
    
    from app.services.historical_context import selected_assignment_for_profile

    try:
        assignment = selected_assignment_for_profile(
            db, profile, body.crew_assignment_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if assignment is None:
        raise HTTPException(status_code=409, detail="No active vessel assignment is available")
    call = _assignment_call_or_conflict(assignment)
    port = call.port_name
    vessel = call.vessel_name
    if not vessel:
        raise HTTPException(
            status_code=409,
            detail="The selected vessel assignment has no vessel context",
        )
    # Caller-supplied labels are display hints only and may not select another
    # operational context.
    if body.vessel_name and body.vessel_name.strip().lower() != vessel.strip().lower():
        raise HTTPException(status_code=409, detail="Vessel does not match selected assignment")
    if body.port_name and port and body.port_name.strip().lower() != port.strip().lower():
        raise HTTPException(status_code=409, detail="Port does not match selected assignment")
    agency_name = call.agency_name

    if not port:
        raise HTTPException(
            status_code=409,
            detail="The selected vessel assignment has no port context",
        )

    if not is_partnered_agency(agency_name):
        return None

    # Derive agent name from port (e.g. "port_singapore" -> "Singapore Port Authority")
    port_display = port.replace("port_", "").replace("_", " ").title()
    agent_name = f"{port_display} Port Authority"

    # Build unique shore pass ID: port code + vessel code + random
    port_code = port.replace("port_", "")[:3].upper()          # e.g. "SIN"
    vessel_code = vessel.replace("vessel_", "V")[:3].upper()   # e.g. "V1"
    random_suffix = uuid.uuid4().hex[:8].upper()
    shore_pass_id = f"SP-{port_code}-{vessel_code}-{random_suffix}"

    # Issue once for legacy profiles that do not yet have an HPID. Existing
    # identities remain stable even when this pass is for a different port.
    ensure_stable_hpid(db, profile, port=port)

    # Generate shore pass
    existing = db.query(ShorePass).filter(
        ShorePass.crew_profile_id == profile.id,
        ShorePass.crew_assignment_id == assignment.id,
    ).order_by(ShorePass.created_at.desc()).first()
    if existing:
        return existing

    new_pass = ShorePass(
        crew_profile_id=profile.id,
        crew_assignment_id=assignment.id,
        vessel_call_id=assignment.vessel_call_id,
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
    crew_assignment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        return None

    from app.services.historical_context import selected_assignment_for_profile
    try:
        assignment = selected_assignment_for_profile(db, profile, crew_assignment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if assignment is None:
        return None
    call = _assignment_call_or_conflict(assignment)
    agency_name = call.agency_name

    if not is_partnered_agency(agency_name):
        return None

    last_pass = db.query(ShorePass).filter(
        ShorePass.crew_profile_id == profile.id,
        ShorePass.crew_assignment_id == assignment.id,
    ).order_by(ShorePass.created_at.desc()).first()
    return last_pass

@router.get("/shorepass/eligibility")
def check_shorepass_eligibility(
    crew_assignment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check if the crew member's vessel is managed by any agent.
    Returns under_agent=true only if the vessel name AND the crew's HPID
    are both found in some agent's vessel_crew mapping AND agency is NOT 'Other'.
    """
    if current_user.role != "crew":
        return {"under_agent": False, "agent_name": None}

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        return {"under_agent": False, "agent_name": None}
    from app.services.historical_context import selected_assignment_for_profile
    try:
        assignment = selected_assignment_for_profile(db, profile, crew_assignment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if assignment is None:
        return {"under_agent": False, "agent_name": None}
    call = _assignment_call_or_conflict(assignment)
    agency_name = call.agency_name

    if not is_partnered_agency(agency_name):
        return {"under_agent": False, "agent_name": None}

    return {
        "under_agent": True,
        "agent_name": agency_name,
        "crew_assignment_id": assignment.id,
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
    
    # Set default times if agent didn't set them
    if not shore_pass.out_time:
        shore_pass.out_time = datetime.now()
    if not shore_pass.expires_at:
        shore_pass.expires_at = _compute_shore_pass_expiry(db, shore_pass.port_name, shore_pass.out_time)
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


class PickupAvailabilityOut(BaseModel):
    available: bool
    reason: Optional[str] = None
    timezone: str
    server_time: datetime
    port_date: str
    port_time: str
    port_day: str
    suggested_pickup_date: str
    suggested_pickup_time: str
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    return_drop_address: Optional[str] = None


def _pickup_availability(
    db: Session,
    port_value: Optional[str],
    scheduled_time: Optional[datetime],
    trip_type: str,
    direction: str = DEFAULT_TRANSFER_DIRECTION,
) -> dict:
    rule = _port_rule_for(db, port_value)
    configured_timezone = rule.timezone if rule else None
    clock = port_clock_snapshot(port_value, configured_timezone)
    port_now = clock["port_now"]

    # Coordinated transfers default to the configured advance-booking buffer;
    # package trips start at the current authoritative port time.
    buffer_minutes = max(0, int(rule.advance_booking_buffer_minutes or 0)) if rule else 30
    suggested_pickup = port_now + timedelta(
        minutes=0 if trip_type == "package_trip" else buffer_minutes
    )
    # Package exploration always means "leave now". Ignore any client-supplied
    # scheduled time so a crafted request cannot move the server clock forward
    # past the port's opening time.
    pickup_at = (
        as_port_local(scheduled_time, clock["zone"])
        if trip_type == "coordinated_transfer" and scheduled_time is not None
        else suggested_pickup
    )

    # A return leg is a ride back to the gate, so opening hours, closing time
    # and non-working days must not block it. Crew stranded ashore after the
    # gate closes are exactly the people who need this booking.
    is_return_leg = (
        trip_type == "coordinated_transfer" and direction == "return_to_port"
    )

    reason = None
    if trip_type == "coordinated_transfer" and scheduled_time is not None and pickup_at < port_now:
        reason = "Pickup time cannot be earlier than the current port time."
    elif rule and not is_return_leg:
        reason = _port_closed_reason(
            pickup_at,
            rule.opening_time,
            rule.closing_time,
            rule.working_days,
        )
        if reason is None and trip_type == "package_trip":
            reason = port_closing_buffer_reason(
                port_now,
                rule.opening_time,
                rule.closing_time,
                PACKAGE_CLOSING_BUFFER_MINUTES,
            )

    return {
        "available": reason is None,
        "reason": reason,
        "timezone": clock["timezone"],
        "server_time": clock["server_time"],
        "port_date": clock["port_date"],
        "port_time": clock["port_time"],
        "port_day": clock["port_day"],
        "suggested_pickup_date": suggested_pickup.strftime("%Y-%m-%d"),
        "suggested_pickup_time": suggested_pickup.strftime("%H:%M"),
        "opening_time": rule.opening_time if rule else None,
        "closing_time": rule.closing_time if rule else None,
        # Only the return screen needs this; skip the lookup otherwise.
        "return_drop_address": _return_drop_address(db, port_value) if is_return_leg else None,
        "pickup_at": pickup_at,
    }


@router.get("/cab/pickup-availability", response_model=PickupAvailabilityOut)
def get_pickup_availability(
    port: Optional[str] = None,
    scheduled_time: Optional[datetime] = None,
    trip_type: str = "coordinated_transfer",
    direction: str = DEFAULT_TRANSFER_DIRECTION,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can check pickup availability")
    if trip_type not in {"coordinated_transfer", "package_trip"}:
        raise HTTPException(status_code=400, detail="Invalid trip type")
    if direction not in TRANSFER_DIRECTIONS:
        raise HTTPException(status_code=400, detail="Invalid transfer direction")
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    return _pickup_availability(
        db,
        profile.current_port or port,
        scheduled_time,
        trip_type,
        direction,
    )


@router.post("/cab/book", response_model=CabBookingCreateOut)
def book_cab(
    body: CabBookingCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    # Resolve retries before checking availability or broadcasting to
    # providers. A retry may arrive after the port window changed, but must
    # still receive the booking originally created for this action.
    server_generated_idempotency_key = not bool(body.idempotency_key)
    idempotency_key = (
        body.idempotency_key.strip()
        if body.idempotency_key
        else f"legacy-{uuid.uuid4().hex}"
    )
    fingerprint_payload = body.model_dump(
        mode="json", exclude={"idempotency_key"}
    )
    request_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    # PostgreSQL transaction-scoped advisory locks serialize identical client
    # actions before any provider notification/timeline side effect is made.
    # SQLite test/dev databases rely on the unique index as the final guard.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        lock_material = hashlib.sha256(
            f"{profile.id}:{idempotency_key}".encode("utf-8")
        ).digest()[:8]
        lock_key = int.from_bytes(lock_material, "big", signed=True)
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
    existing_booking = (
        db.query(CabBooking)
        .filter(
            CabBooking.crew_id == profile.id,
            CabBooking.client_idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing_booking is not None:
        if existing_booking.request_fingerprint != request_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="This idempotency key was already used for a different booking",
            )
        return CabBookingCreateOut(
            booking_id=existing_booking.booking_id,
            otp=existing_booking.otp,
            status=(
                existing_booking.status.value
                if hasattr(existing_booking.status, "value")
                else existing_booking.status
            ),
            agent_number=existing_booking.agent_number,
        )

    from app.services.historical_context import selected_assignment_for_profile

    try:
        booking_assignment = selected_assignment_for_profile(
            db, profile, body.crew_assignment_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if booking_assignment is not None:
        booking_call = _assignment_call_or_conflict(booking_assignment)
        booking_vessel = booking_call.vessel
    else:
        # A vessel no agency runs has no manifest and never will, so there is
        # no assignment to wait for and refusing the booking would lock these
        # crew out of cabs permanently. The profile already says as much —
        # cab_booking_locked is false and no shore pass card is offered — and
        # this is the rest of that promise.
        booking_vessel, booking_call = _unmanaged_vessel_call(db, profile)
        if booking_call is None:
            raise HTTPException(
                status_code=409,
                detail="No active vessel assignment is available for this booking",
            )

    from app.db.models.cab_booking import VehicleType, BookingStatus, RideType
    from app.db.models.booking_timeline import TimelineEventType
    from app.services.booking_service import is_ride_type_available
    from app.services.timeline_service import create_timeline_event

    try:
        ride_type = RideType(body.ride_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ride type")

    # The selected vessel call owns the operation. Device storage and mutable
    # CrewProfile fields cannot choose another port when the person has two
    # concurrent assignments.
    port_value = (
        (booking_call.port.code if booking_call.port else None)
        or booking_call.port_name
    )
    if not port_value and booking_assignment is None:
        # An unmanaged vessel's call carries no port. A call takes its port from
        # the agency's assigned_port, and a vessel recorded as "Other" has no
        # agency to take it from — so the call is opened with the port null and
        # every booking against it died here, one step past the gate that had
        # just let the crew member in.
        #
        # Their own answer is the only one there is: they named a port on Select
        # Port & Vessel and it was stored on their profile. This is safe exactly
        # where the assignment is absent — with an assignment the call still
        # decides, so someone on two ships cannot move a booking to the other.
        port_value = (profile.current_port or "").strip() or None
    if not port_value:
        raise HTTPException(
            status_code=409,
            detail=(
                "No port is recorded for this booking. Set your port on the "
                "Select Port & Vessel screen and try again."
                if booking_assignment is None
                else "The selected vessel assignment has no port context"
            ),
        )
    resolved_trip_type = body.trip_type or (
        "package_trip" if body.scheduled_time is None else "coordinated_transfer"
    )
    if resolved_trip_type not in {"coordinated_transfer", "package_trip"}:
        raise HTTPException(status_code=400, detail="Invalid trip type")

    resolved_direction = body.direction or DEFAULT_TRANSFER_DIRECTION
    if resolved_direction not in TRANSFER_DIRECTIONS:
        raise HTTPException(status_code=400, detail="Invalid transfer direction")

    resolved_drop_address = body.drop_address
    if resolved_direction == "return_to_port":
        # The return exemption must not be usable to book an arbitrary city
        # destination outside port hours. The authenticated profile selects
        # the port, and the server fixes the destination to its main gate.
        resolved_drop_address = _canonical_return_drop_address(db, port_value)

    if resolved_trip_type == "coordinated_transfer":
        if body.scheduled_time is None:
            raise HTTPException(
                status_code=400,
                detail="Scheduled pickup time is required for a coordinated transfer",
            )

    # The API is the authority for all clock decisions. Coordinated transfers
    # validate the selected pickup in the port's timezone; package trips use
    # the server's current port-local time. A changed phone clock cannot bypass
    # this gate.
    pickup_availability = _pickup_availability(
        db,
        port_value,
        body.scheduled_time,
        resolved_trip_type,
        resolved_direction,
    )
    if not pickup_availability["available"]:
        raise HTTPException(status_code=400, detail=pickup_availability["reason"])
    pickup_at = pickup_availability["pickup_at"]

    if resolved_trip_type == "coordinated_transfer" and body.planned_return:
        try:
            _minutes_from_hhmm(body.planned_return)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Planned return time must use HH:MM format",
            )

        port = resolve_port_for_pricing(db, port_value)
        port_candidates = [
            candidate
            for candidate in [
                port.code if port else None,
                port.name if port else None,
                port_value,
            ]
            if candidate
        ]
        port_rule = (
            db.query(PortRule)
            .filter(PortRule.port_name.in_(port_candidates))
            .first()
            if port_candidates
            else None
        )
        closing_time = port_rule.closing_time if port_rule else None
        try:
            return_before_pickup = _planned_return_is_before_pickup(
                pickup_at,
                body.planned_return,
                closing_time,
            )
        except ValueError:
            # A malformed configured closing time must not disable the basic
            # same-day ordering check.
            return_before_pickup = _planned_return_is_before_pickup(
                pickup_at,
                body.planned_return,
            )

        if return_before_pickup:
            raise HTTPException(
                status_code=400,
                detail="Planned return time cannot be earlier than pickup time",
            )

        if port_rule and port_rule.closing_time:
            try:
                return_after_closing = _planned_return_is_after_closing(
                    pickup_at,
                    body.planned_return,
                    port_rule.closing_time,
                )
            except ValueError:
                logger.warning(
                    "Skipping planned-return validation for invalid closing time %r on port %r",
                    port_rule.closing_time,
                    port_value,
                )
            else:
                if return_after_closing:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Planned return time cannot be later than the port "
                            f"closing time ({port_rule.closing_time[:5]})"
                        ),
                    )

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
    otp = str(secrets.randbelow(9000) + 1000)
    now = datetime.utcnow()
    booking_port_rule = _port_rule_for(db, port_value)

    from app.services import agent_contact

    booking_agent_number = agent_contact.support_number_for_assignment(
        db, booking_assignment
    )
    from app.services.historical_context import port_for_reference

    booking_port = port_for_reference(db, port_value)

    new_booking = CabBooking(
        booking_id=booking_id,
        crew_id=profile.id,
        pickup_address=body.pickup_address,
        pickup_lat=body.pickup_lat,
        pickup_lng=body.pickup_lng,
        drop_address=resolved_drop_address,
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
        trip_type=resolved_trip_type,
        provider_id=None,
        aggregator_id=None,
        aggregator_name=None,
        # The ship this trip is taken from, pinned now rather than inferred
        # later — see app/services/agent_contact.py.
        vessel_id=booking_call.vessel_id,
        vessel_call_id=booking_call.id,
        crew_assignment_id=booking_assignment.id if booking_assignment else None,
        agency_id=booking_call.agency_id,
        port_id=booking_call.port_id or (booking_port.id if booking_port else None),
        context_resolution="assignment",
        client_idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        # The agency's own number, not the port's. These two were previously
        # both filled from port_rules.helpline_number, so the "agent number" was
        # really the shared port helpline: an agent editing their contact number
        # changed nothing here, which is why it looked frozen.
        agent_number=booking_agent_number,
        # The helpline is whatever the super admin configured for this port.
        # No placeholder fallback: a made-up number is worse than an honest
        # unavailable state on an emergency contact row.
        helpline_number=booking_port_rule.helpline_number if booking_port_rule else None,
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
    except IntegrityError as exc:
        db.rollback()
        raced_booking = (
            db.query(CabBooking)
            .filter(
                CabBooking.crew_id == profile.id,
                CabBooking.client_idempotency_key == idempotency_key,
            )
            .first()
        )
        if (
            raced_booking is not None
            and raced_booking.request_fingerprint == request_fingerprint
        ):
            return CabBookingCreateOut(
                booking_id=raced_booking.booking_id,
                otp=raced_booking.otp,
                status=(
                    raced_booking.status.value
                    if hasattr(raced_booking.status, "value")
                    else raced_booking.status
                ),
                agent_number=raced_booking.agent_number,
            )
        if raced_booking is None or server_generated_idempotency_key:
            logger.exception(
                "Booking creation failed with a non-idempotency integrity error"
            )
            raise HTTPException(
                status_code=500,
                detail="Unable to create booking",
            ) from exc
        raise HTTPException(
            status_code=409,
            detail="The booking was submitted concurrently; retry with the same idempotency key",
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    try:
        from app.services.whatsapp import (
            notify_crew_package_trip_state,
            notify_crew_coordinated_transfer_trip_state,
            notify_aggregator_trip_request,
            notify_aggregator_coordinated_transfer_trip_request,
        )

        trip_type_label = "Package trip" if resolved_trip_type == "package_trip" else "Coordinated Transfer"
        status_label = STATUS_LABELS.get(new_booking.status, new_booking.status.value)

        if resolved_trip_type == "package_trip":
            duration_label = _extract_package_duration_label(new_booking.vehicle_name)
            notify_crew_package_trip_state(
                current_user.mobile_number, status_label, new_booking.booking_id,
                trip_type_label, new_booking.pickup_address, new_booking.num_passengers, duration_label,
            )
            for eligible_provider in broadcast_providers:
                notify_aggregator_trip_request(
                    eligible_provider.user.mobile_number if eligible_provider.user else None,
                    new_booking.booking_id, trip_type_label, new_booking.pickup_address,
                    new_booking.num_passengers, duration_label,
                )
        else:
            notify_crew_coordinated_transfer_trip_state(
                current_user.mobile_number, status_label, new_booking.booking_id,
                trip_type_label, new_booking.pickup_address, new_booking.drop_address, new_booking.num_passengers,
            )
            for eligible_provider in broadcast_providers:
                notify_aggregator_coordinated_transfer_trip_request(
                    eligible_provider.user.mobile_number if eligible_provider.user else None,
                    new_booking.booking_id, trip_type_label, new_booking.pickup_address,
                    new_booking.drop_address, new_booking.num_passengers,
                )
    except Exception:
        logger.exception("WhatsApp notify failed for booking %s", new_booking.booking_id)

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
    """Get detailed information about a specific booking (owner or invited crew)"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view bookings")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    is_owner = booking.crew_id == profile.id
    if not is_owner:
        inv = db.query(BookingInvitation).filter(
            BookingInvitation.booking_id == booking.id,
            BookingInvitation.invited_crew_id == profile.id,
            BookingInvitation.status == "active",
        ).first()
        if not inv:
            raise HTTPException(status_code=404, detail="Booking not found")
    
    from app.services.booking_service import serialize_booking
    serialized = serialize_booking(booking)
    return CabBookingDetailsOut(
        booking_id=booking.booking_id,
        vehicle_name=booking.vehicle_name,
        estimated_price=float(booking.estimated_price),
        drop_address=booking.drop_address,
        num_passengers=booking.num_passengers,
        driver_name=serialized.get("driver_name") or "Not Yet Assigned",
        driver_phone=serialized.get("driver_phone") or "Not Yet Assigned",
        assigned_driver_id=serialized.get("assigned_driver_id"),
        otp=booking.otp,
        # Historical trip contact is a snapshot from its exact assignment.
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
        created_at=booking.created_at,
        is_owner=is_owner,
        itinerary_stops=serialized.get("itinerary_stops"),
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
    """Cancel a cab booking — only the trip owner can cancel"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can cancel bookings")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    if booking.crew_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the trip owner can cancel this ride")
    
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

    if booking.trip_started_at:
        raise HTTPException(status_code=400, detail="Cannot cancel booking after OTP has been verified")

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
    """Get all cab bookings for the current user (owned + invited)"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can view booking history")
    
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    
    # Get booking IDs where user is invited
    invited_booking_ids = [
        inv.booking_id for inv in db.query(BookingInvitation).filter(
            BookingInvitation.invited_crew_id == profile.id,
            BookingInvitation.status == "active"
        ).all()
    ]
    
    # Fetch bookings: owned OR invited
    from sqlalchemy import or_
    query = db.query(
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
        CabBooking.crew_id,
    ).filter(
        or_(
            CabBooking.crew_id == profile.id,
            CabBooking.id.in_(invited_booking_ids) if invited_booking_ids else False
        )
    ).order_by(CabBooking.created_at.desc(), CabBooking.id.desc())
    
    bookings = query.all()
    
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
            created_at=booking.created_at,
            is_owner=booking.crew_id == profile.id,
        )
        for booking in bookings
    ]


# --- Booking Invitations ---

class CrewInviteIn(BaseModel):
    email: str

class InvitationOut(BaseModel):
    id: int
    booking_id: int
    invited_crew_name: str
    invited_crew_email: str
    invited_by_name: str
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


@router.get("/cab/bookings/{booking_id}/invitations", response_model=List[InvitationOut])
def get_booking_invitations(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all invitations for a booking (owner or invited crew can view)"""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can access this")

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    is_owner = booking.crew_id == profile.id
    is_invited = db.query(BookingInvitation).filter(
        BookingInvitation.booking_id == booking.id,
        BookingInvitation.invited_crew_id == profile.id,
        BookingInvitation.status == "active",
    ).first() is not None

    if not is_owner and not is_invited:
        raise HTTPException(status_code=403, detail="Not part of this booking")

    invitations = db.query(BookingInvitation).filter(
        BookingInvitation.booking_id == booking.id,
        BookingInvitation.status == "active",
    ).all()

    result = []
    for inv in invitations:
        invited_user = db.query(User).filter(User.id == inv.invited_crew.user_id).first() if inv.invited_crew else None
        inviter_user = db.query(User).filter(User.id == inv.invited_by.user_id).first() if inv.invited_by else None
        result.append(InvitationOut(
            id=inv.id,
            booking_id=inv.booking_id,
            invited_crew_name=inv.invited_crew.full_name if inv.invited_crew else "Unknown",
            invited_crew_email=invited_user.email if invited_user else "",
            invited_by_name=inviter_user.name if inviter_user else "Unknown",
            status=inv.status,
            created_at=inv.created_at,
        ))
    return result


@router.post("/cab/bookings/{booking_id}/invite", response_model=InvitationOut)
def invite_crew_to_booking(
    booking_id: str,
    body: CrewInviteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Invite a crew member to a booking by email. Only the owner can invite."""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can invite")

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.crew_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the trip owner can invite crew")

    # Look up the invited user by email
    invitee_user = db.query(User).filter(User.email == body.email.lower().strip()).first()
    if not invitee_user:
        raise HTTPException(status_code=404, detail="No registered crew found with this email")

    invitee_profile = db.query(CrewProfile).filter(CrewProfile.user_id == invitee_user.id).first()
    if not invitee_profile:
        raise HTTPException(status_code=404, detail="This user does not have a crew profile")

    if invitee_profile.id == profile.id:
        raise HTTPException(status_code=400, detail="You cannot invite yourself")

    # Check duplicate
    existing = db.query(BookingInvitation).filter(
        BookingInvitation.booking_id == booking.id,
        BookingInvitation.invited_crew_id == invitee_profile.id,
    ).first()
    if existing:
        if existing.status == "active":
            raise HTTPException(status_code=400, detail="This crew member is already invited")
        else:
            # Re-activate
            existing.status = "active"
            db.commit()
            db.refresh(existing)
            return InvitationOut(
                id=existing.id,
                booking_id=existing.booking_id,
                invited_crew_name=invitee_profile.full_name,
                invited_crew_email=body.email.lower().strip(),
                invited_by_name=profile.full_name,
                status=existing.status,
                created_at=existing.created_at,
            )

    # Check if invitee is already in an active booking (no 2 active trips per crew)
    invitee_active_booking = db.query(CabBooking).join(
        BookingInvitation, BookingInvitation.booking_id == CabBooking.id
    ).filter(
        BookingInvitation.invited_crew_id == invitee_profile.id,
        BookingInvitation.status == "active",
        CabBooking.status.notin_(["completed", "cancelled"]),
    ).first()
    if invitee_active_booking:
        raise HTTPException(status_code=400, detail="This crew member is already part of an active booking")

    invitation = BookingInvitation(
        booking_id=booking.id,
        invited_crew_id=invitee_profile.id,
        invited_by_id=profile.id,
        status="active",
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)

    return InvitationOut(
        id=invitation.id,
        booking_id=invitation.booking_id,
        invited_crew_name=invitee_profile.full_name,
        invited_crew_email=body.email.lower().strip(),
        invited_by_name=profile.full_name,
        status=invitation.status,
        created_at=invitation.created_at,
    )


@router.delete("/cab/bookings/{booking_id}/invite/{invitation_id}")
def remove_booking_invitation(
    booking_id: str,
    invitation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an invitation. Owner can remove any; invited crew can remove themselves."""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can access this")

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    invitation = db.query(BookingInvitation).filter(
        BookingInvitation.id == invitation_id,
        BookingInvitation.booking_id == booking.id,
    ).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    is_owner = booking.crew_id == profile.id
    is_self = invitation.invited_crew_id == profile.id

    if not is_owner and not is_self:
        raise HTTPException(status_code=403, detail="Not authorized to remove this invitation")

    invitation.status = "removed"
    db.commit()

    return {"message": "Invitation removed successfully"}


# ─── Booking Reviews ──────────────────────────────────────────────────────

class BookingReviewIn(BaseModel):
    review_type: str  # "driver" or "facility_stop"
    driver_id: Optional[int] = None
    facility_name: Optional[str] = None
    facility_stop_id: Optional[str] = None
    rating: float = Field(ge=1.0, le=5.0)
    review_text: Optional[str] = None


class BookingReviewOut(BaseModel):
    id: int
    booking_id: str
    review_type: str
    driver_id: Optional[int] = None
    driver_name: Optional[str] = None
    facility_name: Optional[str] = None
    facility_stop_id: Optional[str] = None
    rating: float
    review_text: Optional[str] = None
    crew_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/cab/bookings/{booking_id}/reviews", response_model=BookingReviewOut)
def submit_booking_review(
    booking_id: str,
    body: BookingReviewIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can submit reviews")

    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    raw_status = str(booking.status.value if hasattr(booking.status, 'value') else booking.status).lower()
    if raw_status != "completed":
        raise HTTPException(status_code=400, detail="Can only review completed bookings")

    if body.review_type not in ("driver", "facility_stop"):
        raise HTTPException(status_code=400, detail="review_type must be 'driver' or 'facility_stop'")

    if body.review_type == "driver" and not body.driver_id:
        raise HTTPException(status_code=400, detail="driver_id required for driver reviews")

    from app.db.models.booking_review import BookingReview

    existing = None
    if body.review_type == "driver":
        existing = db.query(BookingReview).filter(
            BookingReview.booking_id == booking.id,
            BookingReview.crew_id == profile.id,
            BookingReview.review_type == "driver",
            BookingReview.driver_id == body.driver_id,
        ).first()
    elif body.review_type == "facility_stop":
        if not body.facility_stop_id:
            raise HTTPException(status_code=400, detail="facility_stop_id required for facility reviews")
        existing = db.query(BookingReview).filter(
            BookingReview.booking_id == booking.id,
            BookingReview.crew_id == profile.id,
            BookingReview.review_type == "facility_stop",
            BookingReview.facility_stop_id == body.facility_stop_id,
        ).first()

    if existing:
        existing.rating = body.rating
        existing.review_text = body.review_text
        if body.facility_name:
            existing.facility_name = body.facility_name
            
        db.flush()
        if body.review_type == "driver" and body.driver_id:
            driver = db.query(Driver).filter(Driver.id == body.driver_id).first()
            if driver:
                avg = db.query(func.avg(BookingReview.rating)).filter(
                    BookingReview.driver_id == body.driver_id,
                    BookingReview.review_type == "driver",
                ).scalar()
                if avg is not None:
                    driver.rating = round(float(avg), 2)

        db.commit()
        db.refresh(existing)
        return _serialize_review(existing, db)

    review = BookingReview(
        booking_id=booking.id,
        crew_id=profile.id,
        review_type=body.review_type,
        driver_id=body.driver_id,
        facility_name=body.facility_name,
        facility_stop_id=body.facility_stop_id,
        rating=body.rating,
        review_text=body.review_text,
    )
    db.add(review)
    db.flush()

    if body.review_type == "driver" and body.driver_id:
        driver = db.query(Driver).filter(Driver.id == body.driver_id).first()
        if driver:
            avg = db.query(func.avg(BookingReview.rating)).filter(
                BookingReview.driver_id == body.driver_id,
                BookingReview.review_type == "driver",
            ).scalar()
            if avg is not None:
                driver.rating = round(float(avg), 2)

    db.commit()
    db.refresh(review)
    return _serialize_review(review, db)


@router.get("/cab/bookings/{booking_id}/reviews", response_model=List[BookingReviewOut])
def get_booking_reviews(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ("crew", "superadmin", "agent", "aggregator"):
        raise HTTPException(status_code=403, detail="Unauthorized")

    booking = db.query(CabBooking).filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    from app.db.models.booking_review import BookingReview
    reviews = db.query(BookingReview).filter(BookingReview.booking_id == booking.id).all()
    return [_serialize_review(r, db) for r in reviews]


def _serialize_review(review, db: Session) -> dict:
    driver_name = None
    if review.driver_id:
        driver = db.query(Driver).filter(Driver.id == review.driver_id).first()
        driver_name = driver.name if driver else None

    crew = db.query(CrewProfile).filter(CrewProfile.id == review.crew_id).first()

    return {
        "id": review.id,
        "booking_id": db.query(CabBooking.booking_id).filter(CabBooking.id == review.booking_id).scalar(),
        "review_type": review.review_type,
        "driver_id": review.driver_id,
        "driver_name": driver_name,
        "facility_name": review.facility_name,
        "facility_stop_id": review.facility_stop_id,
        "rating": review.rating,
        "review_text": review.review_text,
        "crew_name": crew.full_name if crew else None,
        "created_at": review.created_at,
    }
