from app.services.collection_pages import page_all
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    status,
    Form,
    File,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
import os
import shutil
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import String, cast, func, literal, or_, select, union_all
from typing import Any, Dict, List, Literal, Optional

from app.services.result_limits import capped_response
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
import logging
import re

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.restaurant import Restaurant
from app.db.models.hotels import Hotel
from app.db.models.pub import Pub
from app.db.models.vendors import Vendors
from app.db.models.sightseeing import Sightseeing
from app.db.models.crew_profile import CrewProfile
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.driver import Driver
from app.db.models.port import Port
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.incident import Incident
from app.db.models.port_service_request import PortServiceRequest
from app.db.models.contact_message import ContactMessage
from app.db.models.driver_magic_link import DriverMagicLink, DriverMagicLinkReachEvent
from app.db.models.vendor_tag import VendorTag
from app.api.v1.routes_auth import get_current_user
from app.services.vendor_ranking import (
    apply_vendor_commission_ranking,
    categories_for_vendor_section,
    vendor_category_text,
)
from app.services.vendor_data import normalize_vendor_information, validate_coordinates

router = APIRouter()
logger = logging.getLogger(__name__)

# --- Schemas ---

class DashboardStats(BaseModel):
    total_restaurants: int
    total_crew: int
    total_sightseeing: int
    total_pubs: int
    total_hotels: int
    total_massage: int = 0
    total_wellness: int = 0
    total_shopping: int = 0
    total_utility: int = 0
    pending_provider_response: int = 0
    accepted_by_provider: int = 0
    rejected_by_provider: int = 0
    assigned_trips: int = 0
    active_trips: int = 0
    completed_trips: int = 0
    cancelled_trips: int = 0

class VendorBase(BaseModel):
    name: str
    port_id: Optional[int] = None
    lat: float
    lng: float
    rating: float = 0.0
    phone: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None

class RestaurantCreate(VendorBase):
    location_name: str
    distance_from_port: float
    price_per_person: float
    timings: str
    service_type: str
    popular_for: Optional[List[str]] = None
    menu_images: Optional[List[str]] = None
    address: Optional[str] = None

class HotelCreate(VendorBase):
    location: str
    distance_from_port: float
    price_per_night: float
    address: Optional[str] = None

class PubCreate(VendorBase):
    location_name: str
    distance_from_port: float
    price_per_person: float
    timings: str
    service_type: str
    popular_for: Optional[List[str]] = None
    address: Optional[str] = None
    pub_type: Optional[str] = None
    category: Optional[str] = None
    best_for: Optional[str] = None

class SightseeingCreate(VendorBase):
    location_name: str
    distance_from_port: float
    price_per_person: float = 0
    timings: Optional[str] = None
    address: Optional[str] = None
    images: Optional[List[str]] = None

class VendorCreationBase(BaseModel):
    name: str
    category: str
    location_name: str
    distance_from_port: float
    lat: float
    lng: float   
    port_id: Optional[int] = None

class VendorCreate(VendorCreationBase):
    # Optional at creation
    rating: Optional[float] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    documents: Optional[List[str]] = None
    images: Optional[List[str]] = None
    menu_items: Optional[List[str]] = None
    other_information: Optional[Dict[str, Any]] = None
    commission_percentage: float = Field(default=0, ge=0, le=100)


class VendorUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    location_name: Optional[str] = None
    distance_from_port: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    rating: Optional[float] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    documents: Optional[List[str]] = None
    images: Optional[List[str]] = None
    menu_items: Optional[List[str]] = None
    other_information: Optional[Dict[str, Any]] = None
    port_id: Optional[int] = None
    status: Optional[str] = None
    commission_percentage: Optional[float] = Field(default=None, ge=0, le=100)


class VendorOut(BaseModel):
    id: int
    name: str
    category: str
    location_name: str
    distance_from_port: float
    lat: float
    lng: float
    rating: Optional[float]
    phone: Optional[str]
    email: Optional[str]
    status: str
    documents: Optional[List[str]]
    images: Optional[List[str]]
    menu_items: Optional[List[str]] = None
    other_information: Optional[Dict[str, Any]]
    commission_percentage: float
    created_at: datetime
    updated_at: datetime


class VendorTagIn(BaseModel):
    name: str
    slug: Optional[str] = None
    image_url: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


class VendorTagOut(BaseModel):
    id: int
    name: str
    slug: str
    image_url: Optional[str]
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class HistoricalContextResolutionIn(BaseModel):
    vessel_call_id: int
    evidence_type: str
    evidence_reference: Optional[str] = None
    notes: str
    expected_context: Optional[Dict[str, Any]] = None

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str) -> str:
        normalized = value.strip()
        if not 10 <= len(normalized) <= 2000:
            raise ValueError("notes must be between 10 and 2000 non-whitespace characters")
        return normalized


class IdentityConflictResolutionIn(BaseModel):
    expected_version: int = Field(gt=0)
    action: Literal["SELECT_PROFILE", "LEAVE_PENDING", "DISMISS"]
    crew_profile_id: Optional[int] = Field(default=None, gt=0)
    evidence_type: str
    evidence_reference: Optional[str] = Field(default=None, max_length=255)
    reason: str = Field(min_length=10, max_length=2000)

    @field_validator("evidence_type")
    @classmethod
    def trim_required(cls, value: str) -> str:
        normalized = value.strip()
        if not 2 <= len(normalized) <= 64:
            raise ValueError("evidence_type must be between 2 and 64 non-whitespace characters")
        return normalized

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 10:
            raise ValueError("reason must be at least 10 non-whitespace characters")
        return normalized

# --- Helpers ---

def verify_superadmin(current_user: User):
    if current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superadmins can access this resource"
        )


def _identity_conflict_payload(db: Session, row, *, include_audit: bool = False):
    from app.db.models.crew_identity_conflict import CrewIdentityConflictAudit

    candidates = (
        db.query(CrewProfile)
        .filter(CrewProfile.id.in_(row.candidate_profile_ids or []))
        .order_by(CrewProfile.id)
        .all()
    )
    payload = {
        "id": row.id,
        "operation": row.operation,
        "vessel": {
            "id": row.vessel_id,
            "name": row.vessel.name if row.vessel else None,
            "imo_number": row.vessel.imo_number if row.vessel else None,
        },
        # Flat aliases preserve compatibility for an early internal client;
        # new clients should use the nested stable objects below.
        "vessel_id": row.vessel_id,
        "vessel_name": row.vessel.name if row.vessel else None,
        "passport_key": row.passport_key,
        "proposed_identity": row.proposed_identity,
        "candidates": [
            {
                "id": profile.id,
                "hpid": profile.hpid,
                "full_name": profile.full_name,
                "rank": profile.rank,
                "nationality": profile.nationality,
                "passport_number": profile.passport_number,
            }
            for profile in candidates
        ],
        "conflict_message": row.conflict_message,
        "message": row.conflict_message,
        "status": row.status,
        "version": row.version,
        "resolution_action": row.resolution_action,
        "selected_profile_id": row.selected_profile_id,
        "evidence_type": row.evidence_type,
        "evidence_reference": row.evidence_reference,
        "resolution_reason": row.resolution_reason,
        "resolved_by_user_id": row.resolved_by_user_id,
        "resolved_at": row.resolved_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    payload["candidate_profiles"] = payload["candidates"]
    payload["resolution"] = (
        {
            "action": row.resolution_action,
            "selected_profile_id": row.selected_profile_id,
            "evidence_type": row.evidence_type,
            "evidence_reference": row.evidence_reference,
            "reason": row.resolution_reason,
            "resolved_by_user_id": row.resolved_by_user_id,
            "resolved_at": row.resolved_at,
        }
        if row.status == "RESOLVED"
        else None
    )
    if include_audit:
        audits = (
            db.query(CrewIdentityConflictAudit)
            .filter(CrewIdentityConflictAudit.conflict_id == row.id)
            .order_by(
                CrewIdentityConflictAudit.created_at.desc(),
                CrewIdentityConflictAudit.id.desc(),
            )
            .limit(20)
            .all()
        )
        payload["audit_total"] = (
            db.query(CrewIdentityConflictAudit)
            .filter(CrewIdentityConflictAudit.conflict_id == row.id)
            .count()
        )
        payload["audits_truncated"] = payload["audit_total"] > 20
        payload["audits"] = [
            {
                "id": audit.id,
                "actor_user_id": audit.actor_user_id,
                "action": audit.action,
                "expected_version": audit.expected_version,
                "before": audit.before_state,
                "after": audit.after_state,
                "evidence_type": audit.evidence_type,
                "evidence_reference": audit.evidence_reference,
                "reason": audit.reason,
                "created_at": audit.created_at,
            }
            for audit in audits
        ]
    return payload


def slugify_tag(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


def ensure_vendor_tags_table(db: Session) -> None:
    # Lets existing environments start using tags without waiting for migrations.
    VendorTag.__table__.create(bind=db.get_bind(), checkfirst=True)

# --- Routes ---


@router.get("/identity-conflicts")
def list_identity_conflicts(
    status_filter: Literal["OPEN", "RESOLVED", "ALL"] = "OPEN",
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from app.db.models.crew_identity_conflict import CrewIdentityConflictRecord

    page = max(1, page)
    limit = max(1, min(limit, 100))
    query = db.query(CrewIdentityConflictRecord)
    if status_filter != "ALL":
        query = query.filter(CrewIdentityConflictRecord.status == status_filter)
    total = query.count()
    rows = (
        query.order_by(
            CrewIdentityConflictRecord.created_at.desc(),
            CrewIdentityConflictRecord.id.desc(),
        )
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [_identity_conflict_payload(db, row) for row in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/identity-conflicts/{conflict_id}")
def get_identity_conflict(
    conflict_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from app.db.models.crew_identity_conflict import CrewIdentityConflictRecord

    row = db.query(CrewIdentityConflictRecord).filter(
        CrewIdentityConflictRecord.id == conflict_id
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Identity conflict not found")
    return _identity_conflict_payload(db, row, include_audit=True)


@router.post("/identity-conflicts/{conflict_id}/resolve")
def resolve_identity_conflict(
    conflict_id: int,
    body: IdentityConflictResolutionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from app.db.models.crew_identity_conflict import (
        CrewIdentityConflictAudit,
        CrewIdentityConflictRecord,
    )

    row = (
        db.query(CrewIdentityConflictRecord)
        .filter(CrewIdentityConflictRecord.id == conflict_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Identity conflict not found")
    if row.version != body.expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Identity conflict changed; reload before resolving",
                "current_version": row.version,
            },
        )
    if row.status != "OPEN":
        raise HTTPException(status_code=409, detail="Identity conflict is already resolved")
    if body.action == "SELECT_PROFILE":
        if body.crew_profile_id is None:
            raise HTTPException(status_code=422, detail="crew_profile_id is required")
        if body.crew_profile_id not in (row.candidate_profile_ids or []):
            raise HTTPException(
                status_code=409,
                detail="Selected profile is not a candidate for this conflict",
            )
        if db.query(CrewProfile.id).filter(CrewProfile.id == body.crew_profile_id).scalar() is None:
            raise HTTPException(status_code=409, detail="Selected profile no longer exists")
    elif body.crew_profile_id is not None:
        raise HTTPException(
            status_code=422,
            detail="crew_profile_id is valid only for SELECT_PROFILE",
        )

    before = _identity_conflict_payload(db, row)
    row.status = "RESOLVED"
    row.version += 1
    row.resolution_action = body.action
    row.selected_profile_id = body.crew_profile_id
    row.evidence_type = body.evidence_type
    row.evidence_reference = (body.evidence_reference or "").strip() or None
    row.resolution_reason = body.reason
    row.resolved_by_user_id = current_user.id
    row.resolved_at = datetime.utcnow()
    db.flush()

    # Recording the decision is not the same as acting on it. Most operations
    # consult resolved_identity_decision() the next time they run; a
    # historical assignment has no next time, so its decision is applied here
    # or never.
    from app.services.historical_linkage import apply_resolved_conflict

    apply_resolved_conflict(db, row)

    after = _identity_conflict_payload(db, row)
    db.add(CrewIdentityConflictAudit(
        conflict_id=row.id,
        actor_user_id=current_user.id,
        action=body.action,
        expected_version=body.expected_version,
        before_state=jsonable_encoder(before),
        after_state=jsonable_encoder(after),
        evidence_type=body.evidence_type,
        evidence_reference=(body.evidence_reference or "").strip() or None,
        reason=body.reason,
    ))
    db.commit()
    db.refresh(row)
    return _identity_conflict_payload(db, row, include_audit=True)

@router.get("/stats", response_model=DashboardStats)
def get_dashboard_stats(
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    query = db.query(
        Vendors.category,
        func.count(Vendors.id).label("count")
    )

    if port_id:
        query = query.filter(Vendors.port_id == port_id)

    results = query.group_by(Vendors.category).all()

    # 🔹 Convert to dict
    category_counts = {
        str(row.category or "").strip().lower(): row.count for row in results
    }
    # 🔹 Crew logic (unchanged)
    query_crew = db.query(CrewProfile)

    if port_id:
        port_obj = db.query(Port).filter(Port.id == port_id).first()
        port_name = port_obj.name if port_obj else None

        if port_name:
            query_crew = query_crew.filter(
                CrewProfile.current_port.ilike(f"%{port_name}%")
            )

    total_crew = query_crew.count()

    from app.services.booking_service import get_dashboard_metrics
    booking_metrics = get_dashboard_metrics(db, port_id=port_id)

    return DashboardStats(
        total_restaurants=category_counts.get("restaurant", 0),
        total_pubs=category_counts.get("pub", 0),
        total_hotels=category_counts.get("hotel", 0),
        total_sightseeing=category_counts.get("sightseeing", 0),
        total_massage=category_counts.get("massage", 0),
        total_wellness=category_counts.get("wellness", 0),
        total_shopping=category_counts.get("shopping", 0),
        total_utility=category_counts.get("utility", 0),
        total_crew=total_crew,
        **booking_metrics,
    )

# --- CMS Endpoints ---

@router.get("/restaurants")
def list_restaurants(port_id: Optional[int] = None,  search: Optional[str] = None,
db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    query = db.query(Restaurant)
    if port_id:
        query = query.filter(Restaurant.port_id == port_id)
    if search is not None:
        query = query.filter(Restaurant.name.ilike(f"%{search}%"))
    return page_all(
        query, collection="routes_superadmin.list_restaurants.0", label="Restaurants"
    )


@router.post("/restaurants")
def create_restaurant(body: RestaurantCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    data = body.model_dump()
    
    # Check if port exists to avoid 500 on FK violation
    if data.get("port_id"):
        port = db.query(Port).filter(Port.id == data["port_id"]).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {data['port_id']} does not exist")

    # Ensure we only pass fields that exist in the Restaurant model
    db_obj = Restaurant(
        name=data["name"],
        port_id=data.get("port_id"),
        location_name=data["location_name"],
        distance_from_port=data["distance_from_port"],
        rating=data["rating"],
        price_per_person=data["price_per_person"],
        timings=data["timings"],
        service_type=data["service_type"],
        popular_for=data.get("popular_for"),
        phone=data.get("phone"),
        lat=data["lat"],
        lng=data["lng"],
        image_url=data.get("image_url"),
        menu_images=data.get("menu_images"),
        description=data.get("description"),
        address=data.get("address")
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj

@router.get("/hotels")
def list_hotels(port_id: Optional[int] = None,search : Optional[str]=None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    query = db.query(Hotel)
    if port_id:
        query = query.filter(Hotel.port_id == port_id)
    if search is not None:
        query = query.filter(Hotel.name.ilike(f"%{search}%"))
    return page_all(query, collection="routes_superadmin.list_hotels.0", label="Hotels")


@router.post("/hotels")
def create_hotel(body: HotelCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    data = body.model_dump()

    if data.get("port_id"):
        port = db.query(Port).filter(Port.id == data["port_id"]).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {data['port_id']} does not exist")

    db_obj = Hotel(
        name=data["name"],
        port_id=data.get("port_id"),
        location=data["location"],
        distance_from_port=data["distance_from_port"],
        rating=data["rating"],
        price_per_night=data["price_per_night"],
        phone=data.get("phone"),
        lat=data["lat"],
        lng=data["lng"],
        image_url=data.get("image_url"),
        description=data.get("description"),
        address=data.get("address")
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj

@router.get("/pubs")
def list_pubs(port_id: Optional[int] = None, search : Optional[str]=None ,db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    query = db.query(Pub)
    if port_id:
        query = query.filter(Pub.port_id == port_id)
    if search is not None:
        query = query.filter(Pub.name.ilike(f"%{search}%")) 

    return page_all(query, collection="routes_superadmin.list_pubs.0", label="Pubs")


@router.post("/pubs")
def create_pub(body: PubCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    data = body.model_dump()

    if data.get("port_id"):
        port = db.query(Port).filter(Port.id == data["port_id"]).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {data['port_id']} does not exist")

    db_obj = Pub(
        name=data["name"],
        port_id=data.get("port_id"),
        location_name=data["location_name"],
        distance_from_port=data["distance_from_port"],
        rating=data["rating"],
        price_per_person=data["price_per_person"],
        timings=data.get("timings"),
        service_type=data.get("service_type"),
        popular_for=data.get("popular_for"),
        phone=data.get("phone"),
        lat=data["lat"],
        lng=data["lng"],
        image_url=data.get("image_url"),
        description=data.get("description"),
        address=data.get("address")
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj

@router.get("/sightseeing")
def list_sightseeing(port_id: Optional[int] = None, search : Optional[str]=None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    query = db.query(Sightseeing)
    if port_id:
        query = query.filter(Sightseeing.port_id == port_id)
    if search is not None:
        query = query.filter(Sightseeing.name.ilike(f"%{search}%"))

    return page_all(
        query, collection="routes_superadmin.list_sightseeing.0", label="Sightseeing"
    )


@router.post("/sightseeing")
def create_sightseeing(body: SightseeingCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    data = body.model_dump()

    if data.get("port_id"):
        port = db.query(Port).filter(Port.id == data["port_id"]).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {data['port_id']} does not exist")

    db_obj = Sightseeing(
        name=data["name"],
        port_id=data.get("port_id"),
        location_name=data["location_name"],
        distance_from_port=data["distance_from_port"],
        rating=data["rating"],
        price_per_person=data["price_per_person"],
        timings=data.get("timings"),
        phone=data.get("phone"),
        lat=data["lat"],
        lng=data["lng"],
        image_url=data.get("image_url"),
        description=data.get("description"),
        address=data.get("address")
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj

# --- Tracking Endpoints ---

@router.get("/tracking/cab-bookings")
def track_cab_bookings(
    status: Optional[str] = None,
    port_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    provider_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
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
        # The crew's contact number lives on the linked user row.
        User.mobile_number.label("crew_mobile_number"),
        AggregatorProfile.company_name.label("provider_company_name"),
        AggregatorProfile.provider_type.label("provider_type"),
        Driver.name.label("assigned_driver_name"),
        Driver.phone.label("assigned_driver_phone"),
        Driver.vehicle_number.label("assigned_driver_vehicle_number"),
    )
    query = query.outerjoin(CrewProfile, CabBooking.crew_id == CrewProfile.id)
    query = query.outerjoin(User, CrewProfile.user_id == User.id)
    query = query.outerjoin(
        AggregatorProfile,
        or_(
            CabBooking.provider_id == AggregatorProfile.id,
            CabBooking.aggregator_id == AggregatorProfile.id,
        ),
    )
    query = query.outerjoin(Driver, CabBooking.assigned_driver_id == Driver.id)

    if status:
        query = query.filter(cast(CabBooking.status, String) == status.lower())
    if port_id:
        port_obj = db.query(Port).filter(Port.id == port_id).first()
        if port_obj:
            query = query.filter(CabBooking.port.ilike(f"%{port_obj.name}%"))
    if provider_id:
        query = query.filter(
            or_(
                CabBooking.provider_id == provider_id,
                CabBooking.aggregator_id == provider_id,
            )
        )
    if provider_type:
        query = query.filter(AggregatorProfile.provider_type == provider_type)
    if date_from:
        query = query.filter(CabBooking.created_at >= date_from)
    if date_to:
        query = query.filter(CabBooking.created_at <= date_to)

    bookings = page_all(
        query.order_by(CabBooking.created_at.desc(), CabBooking.id.desc()),
        collection="routes_superadmin.track_cab_bookings.0",
        label="Cab bookings",
    )
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
                    "mobile_number": booking.crew_mobile_number,
                },
                "pickup_address": booking.pickup_address,
                "drop_address": booking.drop_address,
                "vehicle_type": (booking.vehicle_type or "").lower() if booking.vehicle_type else None,
                "vehicle_name": booking.vehicle_name,
                "vehicle_category": booking.vehicle_category,
                "estimated_price": float(booking.estimated_price),
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


@router.get("/tracking/magic-links")
def track_magic_links(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    link_query = (
        db.query(
            DriverMagicLink.id,
            DriverMagicLink.token,
            DriverMagicLink.itinerary_stops,
            DriverMagicLink.created_at,
            DriverMagicLink.updated_at,
            CabBooking.id.label("cab_booking_id"),
            CabBooking.booking_id,
            CabBooking.aggregator_name,
            CabBooking.driver_name,
            CabBooking.pickup_address,
            CabBooking.drop_address,
            CrewProfile.full_name.label("crew_name"),
            CrewProfile.hpid.label("crew_hpid"),
        )
        .join(CabBooking, DriverMagicLink.booking_id == CabBooking.id)
        .outerjoin(CrewProfile, CabBooking.crew_id == CrewProfile.id)
        .outerjoin(Driver, CabBooking.assigned_driver_id == Driver.id)
        .outerjoin(
            AggregatorProfile,
            or_(CabBooking.provider_id == AggregatorProfile.id, CabBooking.aggregator_id == AggregatorProfile.id),
        )
    )

    if search:
        pattern = f"%{search}%"
        link_query = link_query.filter(
            or_(
                CrewProfile.full_name.ilike(pattern),
                Driver.name.ilike(pattern),
                AggregatorProfile.company_name.ilike(pattern),
            )
        )

    link_rows = page_all(
        link_query.order_by(
            DriverMagicLink.updated_at.desc(), DriverMagicLink.id.desc()
        ),
        collection="routes_superadmin.track_magic_links.0",
        label="Magic links",
    )
    link_ids = [row.id for row in link_rows]

    events_by_link: Dict[int, List[Dict[str, Any]]] = {}
    reached_stop_ids: Dict[int, set] = {}
    latest_event_by_link: Dict[int, Any] = {}
    if link_ids:
        event_rows = (
            db.query(
                DriverMagicLinkReachEvent.id,
                DriverMagicLinkReachEvent.magic_link_id,
                DriverMagicLinkReachEvent.stop_id,
                DriverMagicLinkReachEvent.stop_name,
                DriverMagicLinkReachEvent.latitude,
                DriverMagicLinkReachEvent.longitude,
                DriverMagicLinkReachEvent.notes,
                DriverMagicLinkReachEvent.reached_at,
            )
            .filter(DriverMagicLinkReachEvent.magic_link_id.in_(link_ids))
            .order_by(DriverMagicLinkReachEvent.reached_at.desc(), DriverMagicLinkReachEvent.id.desc())
            .all()
        )
        for event in event_rows:
            events_by_link.setdefault(event.magic_link_id, []).append(
                {
                    "id": event.id,
                    "stop_id": event.stop_id,
                    "stop_name": event.stop_name,
                    "latitude": event.latitude,
                    "longitude": event.longitude,
                    "notes": event.notes,
                    "reached_at": event.reached_at,
                }
            )
            reached_stop_ids.setdefault(event.magic_link_id, set()).add(event.stop_id)
            if event.magic_link_id not in latest_event_by_link:
                latest_event_by_link[event.magic_link_id] = event

    response: List[Dict[str, Any]] = []
    for row in link_rows:
        itinerary = row.itinerary_stops or []
        latest_event = latest_event_by_link.get(row.id)
        response.append(
            {
                "id": row.id,
                "token": row.token,
                "magic_path": f"/magic-link/{row.token}",
                "booking_id": row.booking_id,
                "aggregator_name": row.aggregator_name,
                "driver_name": row.driver_name,
                "crew_name": row.crew_name,
                "crew_hpid": row.crew_hpid,
                "pickup_address": row.pickup_address,
                "drop_address": row.drop_address,
                "reached_count": len(reached_stop_ids.get(row.id, set())),
                "itinerary_count": len(itinerary),
                "latest_reached_at": latest_event.reached_at if latest_event else None,
                "latest_reached_latitude": latest_event.latitude if latest_event else None,
                "latest_reached_longitude": latest_event.longitude if latest_event else None,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "itinerary": itinerary,
                "events": events_by_link.get(row.id, []),
            }
        )
    return response

@router.get("/tracking/drivers")
def track_drivers(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    return capped_response(
        response, db.query(Driver).order_by(Driver.id.desc()), limit=limit)

@router.get("/tracking/aggregators")
def track_aggregators(
    port_id: Optional[int] = None,
    search: Optional[str] = None,
    provider_type: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        verify_superadmin(current_user)
        query = (
            db.query(AggregatorProfile, User, Port)
            .outerjoin(User, AggregatorProfile.user_id == User.id)
            .outerjoin(Port, AggregatorProfile.operating_port_id == Port.id)
        )
        if port_id:
            query = query.filter(AggregatorProfile.operating_port_id == port_id)
        if provider_type:
            query = query.filter(AggregatorProfile.provider_type == provider_type)
        if status_filter:
            query = query.filter(AggregatorProfile.status == status_filter)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                or_(
                    AggregatorProfile.company_name.ilike(pattern),
                    AggregatorProfile.contact_person.ilike(pattern),
                    AggregatorProfile.aggregator_identifier.ilike(pattern),
                )
            )

        providers = page_all(
            query.order_by(AggregatorProfile.company_name.asc()),
            collection="routes_superadmin.track_aggregators.0",
            label="Aggregators",
        )
        provider_ids = [provider.id for provider, _, _ in providers]
        active_statuses = [
            BookingStatus.PENDING_PROVIDER_RESPONSE.value,
            BookingStatus.PROVIDER_ACCEPTED.value,
            BookingStatus.DRIVER_ASSIGNED.value,
            BookingStatus.DRIVER_ACCEPTED.value,
            BookingStatus.ON_TRIP.value,
            BookingStatus.PENDING.value,
            BookingStatus.CONFIRMED.value,
            BookingStatus.ARRIVED.value,
            BookingStatus.IN_PROGRESS.value,
        ]
        active_booking_counts: Dict[int, int] = {}
        completed_trip_counts: Dict[int, int] = {}
        if provider_ids:
            active_booking_counts = dict(
                db.query(CabBooking.aggregator_id, func.count(CabBooking.id))
                .filter(
                    CabBooking.aggregator_id.in_(provider_ids),
                    cast(CabBooking.status, String).in_(active_statuses),
                )
                .group_by(CabBooking.aggregator_id)
                .all()
            )
            completed_trip_counts = dict(
                db.query(CabBooking.aggregator_id, func.count(CabBooking.id))
                .filter(
                    CabBooking.aggregator_id.in_(provider_ids),
                    cast(CabBooking.status, String) == BookingStatus.COMPLETED.value,
                )
                .group_by(CabBooking.aggregator_id)
                .all()
            )

        driver_counts: Dict[int, Dict[str, int]] = {}
        if provider_ids:
            total_driver_rows = (
                db.query(Driver.aggregator_id, func.count(Driver.id))
                .filter(Driver.aggregator_id.in_(provider_ids))
                .group_by(Driver.aggregator_id)
                .all()
            )
            available_driver_rows = (
                db.query(Driver.aggregator_id, func.count(Driver.id))
                .filter(
                    Driver.aggregator_id.in_(provider_ids),
                    Driver.status == "Available",
                )
                .group_by(Driver.aggregator_id)
                .all()
            )
            for aggregator_id, total_count in total_driver_rows:
                driver_counts[aggregator_id] = {
                    "total_drivers": int(total_count or 0),
                    "available_drivers": 0,
                }
            for aggregator_id, available_count in available_driver_rows:
                driver_counts.setdefault(
                    aggregator_id,
                    {"total_drivers": 0, "available_drivers": 0},
                )["available_drivers"] = int(available_count or 0)

        response: List[Dict[str, Any]] = []
        for provider, user, port in providers:
            counts = driver_counts.get(provider.id, {"total_drivers": 0, "available_drivers": 0})
            response.append(
                {
                    "id": provider.id,
                    "company_name": provider.company_name,
                    "provider_name": provider.company_name,
                    "provider_type": provider.provider_type or "aggregator",
                    "contact_person": provider.contact_person,
                    "operating_port_id": provider.operating_port_id,
                    "operating_port": (
                        {"id": port.id, "name": port.name, "code": port.code}
                        if port
                        else None
                    ),
                    "gst_number": provider.gst_number,
                    "status": provider.status,
                    "profile_image": provider.profile_image,
                    "aggregator_identifier": provider.aggregator_identifier,
                    "fleet": provider.fleet,
                    "documents": provider.documents,
                    "user": (
                        {
                            "id": user.id,
                            "email": user.email,
                            "name": user.name,
                            "mobile_number": user.mobile_number,
                            "role": user.role,
                        }
                        if user
                        else None
                    ),
                    "total_drivers": counts["total_drivers"],
                    "available_drivers": counts["available_drivers"],
                    "active_bookings": active_booking_counts.get(provider.id, 0),
                    "completed_trips": completed_trip_counts.get(provider.id, 0),
                }
            )
        return jsonable_encoder(response)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to load aggregator tracking data: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load aggregator tracking data: {e}",
        )
    # return db.query(AggregatorProfile).options(joinedload(AggregatorProfile.user),joinedload(AggregatorProfile.operating_port)).all()

@router.get("/tracking/incidents")
def track_incidents(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    return capped_response(
        response,
        db.query(Incident).order_by(Incident.created_at.desc()),
        limit=limit,
    )

@router.get("/tracking/crew")
def track_crew(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from sqlalchemy.orm import joinedload

    # Every seafarer in the system, with their account, and no response_model
    # to narrow the columns -- so this is the single largest export of identity
    # data the application can produce in one request.
    return capped_response(
        response,
        # Newest first: a ceiling keeps what it reaches first, so ascending
        # order would silently drop the most recently registered crew.
        db.query(CrewProfile).options(joinedload(CrewProfile.user))
        .order_by(CrewProfile.id.desc()),
        limit=limit,
    )

@router.get("/tracking/service-requests")
def track_service_requests(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    return capped_response(
        response,
        db.query(PortServiceRequest).order_by(PortServiceRequest.created_at.desc()),
        limit=limit,
    )

@router.get("/contact-messages")
def list_contact_messages(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    query = db.query(ContactMessage)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                ContactMessage.email.ilike(pattern),
                ContactMessage.first_name.ilike(pattern),
                ContactMessage.last_name.ilike(pattern),
                ContactMessage.phone.ilike(pattern),
                ContactMessage.message.ilike(pattern),
            )
        )
    return page_all(
        query.order_by(ContactMessage.created_at.desc()),
        collection="routes_superadmin.list_contact_messages.0",
        label="Contact messages",
    )


@router.get("/vendor-tags", response_model=List[VendorTagOut])
def list_vendor_tags(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ensure_vendor_tags_table(db)
    query = db.query(VendorTag)
    if not include_inactive:
        query = query.filter(VendorTag.is_active.is_(True))
    return page_all(
        query.order_by(VendorTag.sort_order.asc(), VendorTag.name.asc()),
        collection="routes_superadmin.list_vendor_tags.0",
        label="Vendor tags",
    )


@router.post("/vendor-tags", response_model=VendorTagOut)
def create_vendor_tag(
    payload: VendorTagIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ensure_vendor_tags_table(db)
    slug = slugify_tag(payload.slug or payload.name)
    if not slug:
        raise HTTPException(status_code=400, detail="Tag slug cannot be empty")
    exists = db.query(VendorTag).filter(VendorTag.slug == slug).first()
    if exists:
        raise HTTPException(status_code=409, detail="Tag already exists")
    tag = VendorTag(
        name=payload.name.strip(),
        slug=slug,
        image_url=(payload.image_url or "").strip() or None,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


@router.put("/vendor-tags/{tag_id}", response_model=VendorTagOut)
def update_vendor_tag(
    tag_id: int,
    payload: VendorTagIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ensure_vendor_tags_table(db)
    tag = db.query(VendorTag).filter(VendorTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    next_slug = slugify_tag(payload.slug or payload.name)
    if not next_slug:
        raise HTTPException(status_code=400, detail="Tag slug cannot be empty")
    dup = db.query(VendorTag).filter(VendorTag.slug == next_slug, VendorTag.id != tag_id).first()
    if dup:
        raise HTTPException(status_code=409, detail="Tag slug already in use")
    tag.name = payload.name.strip()
    tag.slug = next_slug
    tag.image_url = (payload.image_url or "").strip() or None
    tag.is_active = payload.is_active
    tag.sort_order = payload.sort_order
    db.commit()
    db.refresh(tag)
    return tag


@router.delete("/vendor-tags/{tag_id}")
def delete_vendor_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ensure_vendor_tags_table(db)
    tag = db.query(VendorTag).filter(VendorTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    db.delete(tag)
    db.commit()
    return {"ok": True}


@router.post("/vendors", response_model=VendorOut)
def create_place(payload: VendorCreate, db: Session = Depends(get_db),current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    data = payload.model_dump()

    raw_category = str(data.get("category") or "").strip().lower()
    if raw_category not in {"restaurant", "pub", "hotel", "sightseeing", "massage", "wellness", "shopping", "utility"}:
        raise HTTPException(status_code=400, detail="Invalid category")

    port_id = data.get("port_id")
    if port_id is not None:
        if port_id <= 0:
            raise HTTPException(status_code=400, detail="port_id must be a valid port")
        port = db.query(Port).filter(Port.id == port_id).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {port_id} does not exist")

    data["category"] = raw_category
    try:
        validate_coordinates(data["lat"], data["lng"])
        data["other_information"] = normalize_vendor_information(data.get("other_information"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    data["phone"] = (data.get("phone") or "").strip()
    data["email"] = (data.get("email") or "").strip()
    if not data["phone"] or not data["email"]:
        raise HTTPException(status_code=400, detail="Phone and email are required")

    vendor = Vendors(**data)
    
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    return vendor

@router.get("/vendors")
def get_vendors(
    port_id: Optional[int] = None,
    vendor_id: Optional[int] = None,
    category: Optional[str] = None,
    search : Optional[str]=None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from sqlalchemy.orm import joinedload

    query = db.query(Vendors).options(joinedload(Vendors.port))
    if port_id is not None:
        query = query.filter(Vendors.port_id == port_id)

    if vendor_id is not None:
        query = query.filter(Vendors.id == vendor_id)

    if category is not None:
        categories = categories_for_vendor_section(category)
        if not categories:
            raise HTTPException(status_code=400, detail="Invalid category")
        query = query.filter(func.lower(vendor_category_text()).in_(categories))

    if search is not None:
        query = query.filter(Vendors.name.ilike(f"%{search}%"))     

    return page_all(
        apply_vendor_commission_ranking(query),
        collection="routes_superadmin.get_vendors.0",
        label="Vendors",
    )


@router.put("/vendors/{vendor_id}", response_model=VendorOut)
def update_place(
    vendor_id: int,
    payload: VendorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)

    vendor = db.query(Vendors).filter(Vendors.id == vendor_id).first()

    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    patch = payload.model_dump(exclude_unset=True)
    if "category" in patch and patch["category"] is not None:
        patch["category"] = str(patch["category"]).strip().lower()
        if patch["category"] not in {"restaurant", "pub", "hotel", "sightseeing", "massage", "wellness", "shopping", "utility"}:
            raise HTTPException(status_code=400, detail="Invalid category")
    if "phone" in patch and patch["phone"] is None:
        patch["phone"] = ""
    if "email" in patch and patch["email"] is None:
        patch["email"] = ""
    if "commission_percentage" in patch and patch["commission_percentage"] is None:
        raise HTTPException(
            status_code=400,
            detail="commission_percentage cannot be null",
        )
    if "port_id" in patch and patch["port_id"] is not None:
        if patch["port_id"] <= 0:
            raise HTTPException(status_code=400, detail="port_id must be a valid port")
        port = db.query(Port).filter(Port.id == patch["port_id"]).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {patch['port_id']} does not exist")

    try:
        if "other_information" in patch:
            patch["other_information"] = normalize_vendor_information(patch["other_information"])
        validate_coordinates(
            patch.get("lat", vendor.lat),
            patch.get("lng", vendor.lng),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for key, value in patch.items():
        setattr(vendor, key, value)

    db.commit()
    db.refresh(vendor)

    return vendor

# --- Super Admin Agent and Vessel Management ---
from pydantic import EmailStr, Field
import random
import string
from app.db.models.agent_profile import AgentProfile
from app.db.models.vessel import Vessel
from app.services.auth import get_password_hash

class SuperAdminAgentCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    full_name: str
    mobile_number: str
    agency_name: str
    location: str
    assigned_port: Optional[str] = None

class SuperAdminAgentOut(BaseModel):
    id: int
    name: str
    email: str
    mobile_number: Optional[str]
    agency_name: str
    location: str
    agent_identifier: str
    assigned_port: Optional[str] = None
    license_number: Optional[str] = None
    auth_document_url: Optional[str] = None

@router.get("/agents", response_model=List[SuperAdminAgentOut])
def list_agents_superadmin(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    agents = page_all(
        db.query(User).filter(User.role == "agent"),
        collection="routes_superadmin.list_agents_superadmin.0",
        label="Agents superadmin",
    )
    out = []
    for u in agents:
        prof = db.query(AgentProfile).filter(AgentProfile.user_id == u.id).first()
        out.append({
            "id": u.id,
            "name": u.name or u.email,
            "email": u.email,
            "mobile_number": u.mobile_number,
            "agency_name": (prof.agency_name or "") if prof else "",
            "location": (prof.location or "") if prof else "",
            "agent_identifier": (prof.agent_identifier or "") if prof else "",
            "assigned_port": prof.assigned_port if prof else None,
            "license_number": prof.license_number if prof else None,
            "auth_document_url": prof.auth_document_url if prof else None
        })
    return out

class SuperAdminAgentUpdate(BaseModel):
    full_name: Optional[str] = None
    mobile_number: Optional[str] = None
    agency_name: Optional[str] = None
    location: Optional[str] = None
    assigned_port: Optional[str] = None
    license_number: Optional[str] = None

@router.patch("/agents/{agent_id}", response_model=SuperAdminAgentOut)
def update_agent_superadmin(
    agent_id: int,
    body: SuperAdminAgentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)

    user = db.query(User).filter(User.id == agent_id, User.role == "agent").first()
    if not user:
        raise HTTPException(status_code=404, detail="Agent not found")

    if body.full_name is not None:
        user.name = body.full_name
    if body.mobile_number is not None:
        user.mobile_number = body.mobile_number

    agent_profile = db.query(AgentProfile).filter(AgentProfile.user_id == agent_id).first()
    if not agent_profile:
        raise HTTPException(status_code=404, detail="Agent profile not found")

    if body.agency_name is not None:
        agent_profile.agency_name = body.agency_name
    if body.location is not None:
        agent_profile.location = body.location
    if body.assigned_port is not None:
        agent_profile.assigned_port = body.assigned_port
    if body.license_number is not None:
        agent_profile.license_number = body.license_number

    try:
        db.commit()
        db.refresh(user)
        db.refresh(agent_profile)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "mobile_number": user.mobile_number,
        "agency_name": agent_profile.agency_name,
        "location": agent_profile.location,
        "agent_identifier": agent_profile.agent_identifier,
        "assigned_port": agent_profile.assigned_port,
        "license_number": agent_profile.license_number,
        "auth_document_url": agent_profile.auth_document_url
    }

@router.post("/agents", response_model=SuperAdminAgentOut, status_code=status.HTTP_201_CREATED)
def create_agent_superadmin(
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    mobile_number: str = Form(...),
    agency_name: str = Form(...),
    location: str = Form(...),
    assigned_port: Optional[str] = Form(None),
    license_number: Optional[str] = Form(None),
    auth_document: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    email = email.lower().strip()

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    if mobile_number and db.query(User).filter(User.mobile_number == mobile_number).first():
        raise HTTPException(status_code=409, detail="Mobile number already registered")

    # Handle File Upload
    document_url = None
    if auth_document:
        os.makedirs("uploads", exist_ok=True)
        ext = os.path.splitext(auth_document.filename)[1]
        filename = f"agent_doc_{uuid.uuid4().hex}{ext}"
        filepath = os.path.join("uploads", filename)
        with open(filepath, "wb") as buffer:
            shutil.copyfileobj(auth_document.file, buffer)
        document_url = f"/uploads/{filename}"

    # 1. Create User
    user = User(
        name=full_name,
        email=email,
        mobile_number=mobile_number,
        hashed_password=get_password_hash(password),
        role="agent",
        must_change_password=True
    )
    db.add(user)
    db.flush()

    # 2. Create Agent Profile
    rand_part = ''.join(random.choices(string.digits, k=4))
    agent_id = f"AGT-{random.randint(10000, 99999)}-{rand_part}"

    agent_profile = AgentProfile(
        user_id=user.id,
        agency_name=agency_name,
        contact_person=full_name,
        location=location,
        agent_identifier=agent_id,
        assigned_port=assigned_port,
        license_number=license_number,
        auth_document_url=document_url
    )
    db.add(agent_profile)
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    db.refresh(user)
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "mobile_number": user.mobile_number,
        "agency_name": agent_profile.agency_name,
        "location": agent_profile.location,
        "agent_identifier": agent_profile.agent_identifier,
        "assigned_port": agent_profile.assigned_port,
        "license_number": agent_profile.license_number,
        "auth_document_url": agent_profile.auth_document_url
    }

class SuperAdminVesselCreate(BaseModel):
    name: str
    imo_number: str
    vessel_type: str
    berth_assignment: Optional[str] = None
    flag: Optional[str] = None
    agency_name: Optional[str] = None
    agent_id: Optional[int] = None
    # Accepted for backwards compatibility; vessel rosters own this value.
    crew_count: Optional[int] = None
    total_crew: Optional[int] = None
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: Literal["Active"] = "Active"

from app.api.v1.routes_vessels import VesselOut, is_partnered_agency, vessel_out

@router.get("/vessels", response_model=List[VesselOut])
def list_all_vessels_superadmin(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    vessels = page_all(
        db.query(Vessel).order_by(Vessel.id.desc()),
        collection="routes_superadmin.list_all_vessels_superadmin.0",
        label="All vessels superadmin",
    )
    output = []
    for vessel in vessels:
        serialized = vessel_out(vessel)
        if not serialized.agency_name:
            serialized = serialized.model_copy(update={"agency_name": "Other"})
        output.append(serialized)
    return output

def _agency_key(name) -> str:
    """Agency names compared the way a person reads them."""
    return (name or "").strip().lower()


def _agent_for_new_vessel(db: Session, body, current_user: User) -> int:
    """Which agent a superadmin-created vessel belongs to.

    This used to fall back to the superadmin's own id whenever the agency name
    failed to match a profile, and the form does not send an agent id — so a
    vessel assigned to a real agency was created successfully, reported as
    created, and belonged to nobody who could see it. It appeared in neither
    the agent's dashboard nor the agency's mapped vessels.

    An agency that names no partner is a genuine case and still rests with the
    superadmin. An agency that names one and cannot be found is not: that is
    refused, because silently keeping the vessel is what hid it.
    """
    from app.db.models.agent_profile import AgentProfile

    if body.agent_id:
        agent = db.query(User).filter(
            User.id == body.agent_id, User.role == "agent"
        ).first()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent user not found")
        return agent.id

    if not is_partnered_agency(body.agency_name):
        return current_user.id

    # Matched on the trimmed, case-folded name. The form's options come from the
    # stored names, so an exact comparison should hold — but a difference in
    # spacing or case used to mean the vessel quietly went to the superadmin,
    # and that failure is not worth preserving.
    wanted = _agency_key(body.agency_name)
    profiles = db.query(AgentProfile).filter(
        func.lower(func.trim(AgentProfile.agency_name)) == wanted
    ).all()
    if not profiles:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No agency is registered under the name '{body.agency_name}'. "
                "Create the agency first, or choose 'Other' to hold the vessel "
                "without assigning it."
            ),
        )
    if len({p.user_id for p in profiles}) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"More than one agency is registered as '{body.agency_name}'. "
                "Assign the vessel from that agency's own row instead."
            ),
        )
    return profiles[0].user_id


@router.post("/vessels", response_model=VesselOut, status_code=status.HTTP_201_CREATED)
def create_vessel_superadmin(
    body: SuperAdminVesselCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    assigned_agent_id = _agent_for_new_vessel(db, body, current_user)

    # A ship that comes back is the same ship, whoever adds it. The agent path
    # has reused the canonical vessel since the returning-vessel work; this one
    # still built a second Vessel unconditionally, so the unique IMO index
    # rejected the insert and the handler below reported "Vessel IMO possibly
    # already exists" — which named no vessel, offered no next step, and left
    # the superadmin unable to record the new call at all.
    from app.api.v1.routes_vessels import _start_return_call, _vessel_by_imo

    returning = _vessel_by_imo(db, body.imo_number)
    if returning is not None:
        return _start_return_call(
            db, returning, body,
            agent_id=assigned_agent_id, agency_name=body.agency_name,
        )

    vessel = Vessel(
        agent_id=assigned_agent_id,
        name=body.name,
        imo_number=body.imo_number,
        vessel_type=body.vessel_type,
        berth_assignment=body.berth_assignment,
        flag=body.flag,
        agency_name=body.agency_name or "Other",
        crew_count=0,
        eta=body.eta,
        etd=body.etd,
        status="Active"
    )
    db.add(vessel)
    try:
        db.flush()
        from app.services.historical_context import active_vessel_call
        from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

        active_vessel_call(db, vessel)
        synchronize_vessel_lifecycle(db, [vessel])
        db.commit()
        db.refresh(vessel)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Vessel IMO possibly already exists")

    if not vessel.agency_name and vessel.agent and hasattr(vessel.agent, "agent_profile") and vessel.agent.agent_profile:
        vessel.agency_name = vessel.agent.agent_profile.agency_name

    return vessel

@router.patch("/vessels/{vessel_id}", response_model=VesselOut)
def update_vessel_superadmin(
    vessel_id: int,
    body: SuperAdminVesselCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")

    vessel.name = body.name
    vessel.imo_number = body.imo_number
    vessel.vessel_type = body.vessel_type
    vessel.berth_assignment = body.berth_assignment
    vessel.flag = body.flag
    # Changing the agency moves the vessel; resubmitting the same one does not.
    #
    # The form sends the agency name on every save and never sends an agent id,
    # so requiring an explicit id meant the dropdown only ever relabelled the
    # vessel: agency_name said one agency and agent_id still held another, and
    # since visibility follows agent_id the vessel appeared on no dashboard and
    # in no agency's mapped vessels. That is what "the assignment is not
    # visible" was.
    #
    # Comparing against the stored name is what keeps the earlier fix intact —
    # an unchanged name resubmitted while correcting an ETD is not a
    # reassignment and must not be read as one.
    if body.agent_id:
        vessel.agent_id = body.agent_id
        if body.agency_name is not None:
            vessel.agency_name = body.agency_name
    elif body.agency_name is not None:
        renamed = _agency_key(body.agency_name) != _agency_key(vessel.agency_name)
        vessel.agency_name = body.agency_name
        if renamed:
            vessel.agent_id = _agent_for_new_vessel(db, body, current_user)

    vessel.eta = body.eta
    vessel.etd = body.etd

    from app.services.historical_context import refresh_active_vessel_call
    from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

    # Editing never opens or closes a port call.
    #
    # This previously read a changed agent_id as a reassignment: it finished the
    # current call and opened a new one, so editing an ETD started a fresh port
    # call. On an archived vessel it was worse — archiving nulls agent_id, so
    # the condition always fired, and because a call cannot be manufactured for
    # a departed vessel the old call was closed with nothing opened in its
    # place, leaving the vessel with no active call at all.
    #
    # Moving a vessel between agencies is a real event that does need a new
    # call, but it is a deliberate transition, not a side effect of saving a
    # form.
    refresh_active_vessel_call(db, vessel)
    synchronize_vessel_lifecycle(db, [vessel])

    try:
        db.commit()
        db.refresh(vessel)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Failed to update vessel")

    if not vessel.agency_name and vessel.agent and hasattr(vessel.agent, "agent_profile") and vessel.agent.agent_profile:
        vessel.agency_name = vessel.agent.agent_profile.agency_name

    return vessel

def _archive_vessel(db: Session, vessel: Vessel) -> VesselOut:
    """Remove a vessel from current operations without destroying history."""
    from app.services.historical_context import finish_vessel_call

    finish_vessel_call(db, vessel, status="ARCHIVED")
    vessel.agent_id = None
    vessel.status = "Archived"
    db.commit()
    db.refresh(vessel)
    return vessel_out(vessel)


@router.post("/vessels/{vessel_id}/archive", response_model=VesselOut)
def archive_vessel_superadmin(
    vessel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")

    return _archive_vessel(db, vessel)


@router.delete("/vessels/{vessel_id}", status_code=status.HTTP_409_CONFLICT)
def delete_vessel_superadmin(
    vessel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reject destructive deletion; the archive endpoint is the safe lifecycle action."""
    verify_superadmin(current_user)
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Vessel hard deletion is disabled. Archive the vessel to preserve operational history.",
    )

@router.post("/agents/{agent_id}/vessels", response_model=VesselOut, status_code=status.HTTP_201_CREATED)
def create_vessel_under_agent(
    agent_id: int,
    body: SuperAdminVesselCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    agent = db.query(User).filter(User.id == agent_id, User.role == "agent").first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent user not found")

    # A ship that comes back is the same ship, whoever adds it. The other two
    # entry points reuse the canonical vessel and open a new call against it;
    # this one still built a second Vessel unconditionally, so a return visit
    # hit the unique IMO index and came back as "Vessel IMO possibly already
    # exists" — with no vessel created and nothing said about why.
    from app.api.v1.routes_vessels import _start_return_call, _vessel_by_imo

    returning = _vessel_by_imo(db, body.imo_number)
    if returning is not None:
        return _start_return_call(
            db, returning, body,
            agent_id=agent.id,
            agency_name=agent.agent_profile.agency_name if agent.agent_profile else None,
        )

    vessel = Vessel(
        agent_id=agent.id,
        name=body.name,
        imo_number=body.imo_number,
        vessel_type=body.vessel_type,
        berth_assignment=body.berth_assignment,
        flag=body.flag,
        agency_name=agent.agent_profile.agency_name if agent.agent_profile else "Other",
        crew_count=0,
        eta=body.eta,
        etd=body.etd,
        status="Active"
    )
    db.add(vessel)
    try:
        db.flush()
        from app.services.historical_context import active_vessel_call
        from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

        active_vessel_call(db, vessel)
        synchronize_vessel_lifecycle(db, [vessel])
        db.commit()
        db.refresh(vessel)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Vessel IMO possibly already exists")
    
    return vessel

@router.get("/agents/{agent_id}/vessels", response_model=List[VesselOut])
def list_agent_vessels_superadmin(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    verify_superadmin(current_user)
    agent = db.query(User).filter(User.id == agent_id, User.role == "agent").first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent user not found")
        
    from app.db.models.vessel_call import VesselCall
    # Retain previous handling relationships after a ship changes agency.
    historical_ids = db.query(VesselCall.vessel_id).filter(
        VesselCall.agency_id == agent.agent_profile.id
    ) if agent.agent_profile else db.query(Vessel.id).filter(False)
    vessels = db.query(Vessel).filter(or_(
        Vessel.agent_id == agent.id, Vessel.id.in_(historical_ids)
    )).all()
    from app.services.vessel_lifecycle import current_call_predicate
    current_ids = set(row[0] for row in db.query(VesselCall.vessel_id).filter(
        VesselCall.agency_id == agent.agent_profile.id,
        current_call_predicate(),
    )) if agent.agent_profile else set()
    output = []
    for vessel in vessels:
        mapped_call = db.query(VesselCall).filter(
            VesselCall.vessel_id == vessel.id,
            VesselCall.agency_id == agent.agent_profile.id,
        ).order_by(VesselCall.id.desc()).first() if agent.agent_profile else None
        current = vessel.id in current_ids
        updates = {"agency_mapping_context": "current" if current else "historical",
            "agency_mapping_call_id": mapped_call.id if mapped_call else None}
        if mapped_call is not None:
            updates.update(eta=mapped_call.eta, etd=mapped_call.etd, agency_name=mapped_call.agency_name)
            if not current:
                updates["status"] = "Archived" if str(mapped_call.status).upper() == "ARCHIVED" else "Departed"
        output.append(vessel_out(vessel).model_copy(update=updates))
    return output


@router.get("/reviews")
def list_all_reviews(
    review_type: Optional[str] = None,
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)

    from app.db.models.booking_review import BookingReview
    from app.db.models.cab_booking import CabBooking as CabBookingModel
    from app.db.models.crew_profile import CrewProfile as CrewProfileModel
    from app.db.models.driver import Driver as DriverModel

    query = (
        db.query(
            BookingReview.id,
            BookingReview.review_type,
            BookingReview.rating,
            BookingReview.review_text,
            BookingReview.facility_name,
            BookingReview.facility_stop_id,
            BookingReview.created_at,
            CabBookingModel.booking_id.label("booking_id"),
            CabBookingModel.port,
            CabBookingModel.vehicle_name,
            CabBookingModel.estimated_price,
            CrewProfileModel.full_name.label("crew_name"),
            CrewProfileModel.hpid.label("crew_hpid"),
            DriverModel.name.label("driver_name"),
            DriverModel.phone.label("driver_phone"),
        )
        .outerjoin(CabBookingModel, BookingReview.booking_id == CabBookingModel.id)
        .outerjoin(CrewProfileModel, BookingReview.crew_id == CrewProfileModel.id)
        .outerjoin(DriverModel, BookingReview.driver_id == DriverModel.id)
    )

    if review_type:
        query = query.filter(BookingReview.review_type == review_type)
    if port_id:
        port_obj = db.query(Port).filter(Port.id == port_id).first()
        if port_obj:
            query = query.filter(CabBookingModel.port.ilike(f"%{port_obj.name}%"))

    rows = page_all(
        query.order_by(BookingReview.created_at.desc()),
        collection="routes_superadmin.list_all_reviews.0",
        label="All reviews",
    )

    return [
        {
            "id": row.id,
            "review_type": row.review_type,
            "rating": row.rating,
            "review_text": row.review_text,
            "facility_name": row.facility_name,
            "facility_stop_id": row.facility_stop_id,
            "booking_id": row.booking_id,
            "port": row.port,
            "vehicle_name": row.vehicle_name,
            "estimated_price": (
                float(row.estimated_price) if row.estimated_price else None
            ),
            "crew_name": row.crew_name,
            "crew_hpid": row.crew_hpid,
            "driver_name": row.driver_name,
            "driver_phone": row.driver_phone,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/reviews/stats")
def review_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)

    from app.db.models.booking_review import BookingReview
    from sqlalchemy import func as sqlfunc

    total = db.query(sqlfunc.count(BookingReview.id)).scalar() or 0
    avg_rating = db.query(sqlfunc.avg(BookingReview.rating)).scalar()
    driver_avg = db.query(sqlfunc.avg(BookingReview.rating)).filter(
        BookingReview.review_type == "driver"
    ).scalar()
    facility_avg = db.query(sqlfunc.avg(BookingReview.rating)).filter(
        BookingReview.review_type == "facility_stop"
    ).scalar()
    driver_count = db.query(sqlfunc.count(BookingReview.id)).filter(
        BookingReview.review_type == "driver"
    ).scalar() or 0
    facility_count = db.query(sqlfunc.count(BookingReview.id)).filter(
        BookingReview.review_type == "facility_stop"
    ).scalar() or 0

    return {
        "total_reviews": total,
        "avg_rating": round(float(avg_rating), 2) if avg_rating else None,
        "driver_avg_rating": round(float(driver_avg), 2) if driver_avg else None,
        "driver_review_count": driver_count,
        "facility_avg_rating": round(float(facility_avg), 2) if facility_avg else None,
        "facility_review_count": facility_count,
    }


# =====================================================
# EXPENSE BILLS (crew-uploaded receipts) — admin view
# =====================================================
from app.db.models.expense_bill import ExpenseBill
from app.db.models.shore_pass import ShorePass
from app.services import storage as bill_storage


class AdminBillOut(BaseModel):
    id: int
    crew_id: int
    crew_name: Optional[str] = None
    merchant: str
    # Admin-facing amount is the PRE-TAX figure; falls back to the paid total
    # for bills uploaded before the tax split existed.
    amount_pre_tax: Optional[float] = None
    amount_post_tax: Optional[float] = None
    bill_number: Optional[str] = None
    bill_date: Optional[datetime] = None
    trip_kind: Optional[str] = None       # "shore_pass" | "cab_booking" | None
    trip_label: Optional[str] = None
    receipt_url: str
    receipt_filename: str
    created_at: datetime


@router.get("/expense-bills", response_model=List[AdminBillOut])
def admin_list_expense_bills(
    crew_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All crew bills for the super admin, showing pre-tax amounts."""
    verify_superadmin(current_user)

    q = db.query(ExpenseBill).order_by(ExpenseBill.created_at.desc())
    if crew_id is not None:
        q = q.filter(ExpenseBill.crew_id == crew_id)
    bills = q.limit(500).all()

    crew_names = {
        cp.id: cp.full_name
        for cp in db.query(CrewProfile).filter(
            CrewProfile.id.in_({b.crew_id for b in bills} or {0})
        ).all()
    }
    sp_labels = {
        sp.id: f"Shore leave {sp.shore_pass_id}"
        for sp in db.query(ShorePass).filter(
            ShorePass.id.in_({b.shore_pass_id for b in bills if b.shore_pass_id} or {0})
        ).all()
    }
    cab_labels = {
        cb.id: f"Cab {cb.booking_id}"
        for cb in db.query(CabBooking).filter(
            CabBooking.id.in_({b.cab_booking_id for b in bills if b.cab_booking_id} or {0})
        ).all()
    }

    out: List[AdminBillOut] = []
    for b in bills:
        pre = b.amount_pre_tax if b.amount_pre_tax is not None else (b.amount_post_tax or b.amount)
        post = b.amount_post_tax if b.amount_post_tax is not None else b.amount
        trip_kind = trip_label = None
        if b.shore_pass_id:
            trip_kind, trip_label = "shore_pass", sp_labels.get(b.shore_pass_id)
        elif b.cab_booking_id:
            trip_kind, trip_label = "cab_booking", cab_labels.get(b.cab_booking_id)
        out.append(AdminBillOut(
            id=b.id,
            crew_id=b.crew_id,
            crew_name=crew_names.get(b.crew_id),
            merchant=b.merchant,
            amount_pre_tax=float(pre) if pre is not None else None,
            amount_post_tax=float(post) if post is not None else None,
            bill_number=b.bill_number,
            bill_date=b.bill_date,
            trip_kind=trip_kind,
            trip_label=trip_label,
            receipt_url=bill_storage.resolve(b.receipt_url),
            receipt_filename=b.receipt_filename,
            created_at=b.created_at,
        ))
    return out


# --- Registrations & logins ------------------------------------------------

# Users and drivers live in separate tables, so "everyone who registered" is the
# union of the two. Roles are listed explicitly to give a stable column order
# and to surface a role with zero rows rather than dropping it.
ACCOUNT_ROLES = ["crew", "agent", "aggregator", "driver", "superadmin"]


@router.get("/user-metrics")
def get_user_metrics(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """How many accounts were registered, and how many logins happened.

    Registrations are counted from `users.created_at` / `drivers.created_at`, so
    they cover the whole history. Logins come from `login_events`, which did not
    exist before this feature — there is no way to reconstruct earlier logins,
    so the response reports when tracking began and the screen says so.
    """
    verify_superadmin(current_user)

    from app.db.models.login_event import LoginEvent

    def parse(value, label):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD")

    start = parse(start_date, "start_date")
    end = parse(end_date, "end_date")
    if end:
        # Inclusive of the whole end day, which is what a date picker implies.
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="start_date is after end_date")

    def in_range(query, column):
        if start:
            query = query.filter(column >= start)
        if end:
            query = query.filter(column <= end)
        return query

    # Registrations ---------------------------------------------------------
    registrations = {role: 0 for role in ACCOUNT_ROLES}
    user_rows = in_range(
        db.query(User.role, func.count(User.id)), User.created_at
    ).group_by(User.role).all()
    for role, count in user_rows:
        key = (role or "").strip().lower()
        registrations[key] = registrations.get(key, 0) + count

    driver_registrations = in_range(db.query(Driver.id), Driver.created_at).count()
    registrations["driver"] = registrations.get("driver", 0) + driver_registrations

    # Logins ----------------------------------------------------------------
    logins = {role: 0 for role in ACCOUNT_ROLES}
    login_rows = in_range(
        db.query(LoginEvent.role, func.count(LoginEvent.id)), LoginEvent.created_at
    ).group_by(LoginEvent.role).all()
    for role, count in login_rows:
        key = (role or "").strip().lower()
        logins[key] = logins.get(key, 0) + count

    # Distinct people, not login count — one crew member signing in 40 times is
    # one person, and the two numbers together say whether use is broad or deep.
    distinct_users = in_range(
        db.query(func.count(func.distinct(LoginEvent.user_id))).filter(
            LoginEvent.user_id.isnot(None)
        ),
        LoginEvent.created_at,
    ).scalar() or 0
    distinct_drivers = in_range(
        db.query(func.count(func.distinct(LoginEvent.driver_id))).filter(
            LoginEvent.driver_id.isnot(None)
        ),
        LoginEvent.created_at,
    ).scalar() or 0

    tracking_started = db.query(func.min(LoginEvent.created_at)).scalar()

    return {
        "roles": ACCOUNT_ROLES,
        "registrations": {
            "total": sum(registrations.values()),
            "by_role": registrations,
        },
        "logins": {
            "total": sum(logins.values()),
            "by_role": logins,
            "distinct_accounts": distinct_users + distinct_drivers,
            # Null until the first login is recorded. The screen uses this to
            # explain a zero rather than presenting it as a real decline.
            "tracking_started_at": tracking_started,
        },
        "start_date": start_date,
        "end_date": end_date,
    }


HISTORICAL_EVIDENCE_TYPES = {
    "trip_record",
    "vessel_record",
    "agency_confirmation",
    "manual_document",
}


def _context_dict(record) -> dict:
    return {
        "vessel_id": record.vessel_id,
        "vessel_call_id": record.vessel_call_id,
        "agency_id": record.agency_id,
        "crew_assignment_id": record.crew_assignment_id,
        "port_id": record.port_id,
        "context_resolution": record.context_resolution,
    }


def _needs_context_reconciliation(record, call) -> bool:
    from app.services.event_origin import classify, CONFIRMED

    return classify(record, call) not in CONFIRMED


def _unresolved_context_filter(model, vessel_call_model):
    from app.services.event_origin import resolved_origin

    return ~resolved_origin(model)


def _unresolved_context_select(model, vessel_call_model, *, kind: str):
    from app.services.event_origin import origin_expression

    reference = (
        model.incident_id
        if kind == "incident"
        else (
            model.booking_id
            if kind == "booking"
            else literal("SOS-") + cast(model.id, String)
        )
    )
    title = (
        model.title
        if kind == "incident"
        else literal("Cab booking") if kind == "booking" else literal("Crew SOS alert")
    )
    reporter = (
        model.reporter_id
        if kind == "incident"
        else literal(None) if kind == "booking" else model.crew_email
    )
    return (
        select(
            literal(kind).label("record_kind"),
            model.id.label("record_id"),
            reference.label("reference"),
            model.created_at.label("created_at"),
            title.label("title"),
            (model.booking_id if kind == "booking" else model.trip_id).label("trip_id"),
            reporter.label("reporter_reference"),
            (model.port if kind == "booking" else model.port_name).label("port_name"),
            model.vessel_id.label("vessel_id"),
            model.vessel_call_id.label("vessel_call_id"),
            model.agency_id.label("agency_id"),
            model.crew_assignment_id.label("crew_assignment_id"),
            model.port_id.label("port_id"),
            model.context_resolution.label("context_resolution"),
            origin_expression(model).label("origin_classification"),
        )
        .outerjoin(vessel_call_model, vessel_call_model.id == model.vessel_call_id)
        .where(_unresolved_context_filter(model, vessel_call_model))
    )


@router.get("/historical-context/unresolved")
def list_unresolved_historical_context(
    record_kind: Optional[str] = None,
    record_limit: int = 100,
    record_offset: int = 0,
    vessel_call_limit: int = 500,
    vessel_call_offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Superadmin work queue; candidates are choices, never automatic matches."""
    verify_superadmin(current_user)
    from app.db.models.agent_profile import AgentProfile
    from app.db.models.crew_sos import CrewSos
    from app.db.models.vessel_call import VesselCall

    kind = (record_kind or "").strip().lower()
    if kind and kind not in {"incident", "sos", "booking"}:
        raise HTTPException(
            status_code=400, detail="record_kind must be incident, sos or booking"
        )

    if not 1 <= record_limit <= 500 or record_offset < 0:
        raise HTTPException(status_code=422, detail="Invalid record pagination")
    if not 1 <= vessel_call_limit <= 500 or vessel_call_offset < 0:
        raise HTTPException(status_code=422, detail="Invalid vessel-call pagination")

    record_selects = []
    if kind in {"", "incident"}:
        record_selects.append(_unresolved_context_select(Incident, VesselCall, kind="incident"))
    if kind in {"", "sos"}:
        record_selects.append(_unresolved_context_select(CrewSos, VesselCall, kind="sos"))

    if kind in {"", "booking"}:
        record_selects.append(
            _unresolved_context_select(CabBooking, VesselCall, kind="booking")
        )

    unresolved = (
        union_all(*record_selects).subquery()
        if len(record_selects) > 1
        else record_selects[0].subquery()
    )
    record_total = db.execute(select(func.count()).select_from(unresolved)).scalar_one()
    record_rows = db.execute(
        select(unresolved)
        .order_by(
            unresolved.c.created_at.is_(None),
            unresolved.c.created_at.desc(),
            unresolved.c.record_id.desc(),
        )
        .offset(record_offset)
        .limit(record_limit)
    ).mappings().all()

    calls_query = db.query(VesselCall).filter(
        VesselCall.vessel_id.isnot(None),
    ).order_by(VesselCall.started_at.desc(), VesselCall.id.desc())
    vessel_call_total = calls_query.count()
    calls = calls_query.offset(vessel_call_offset).limit(vessel_call_limit).all()

    agencies = {
        row.id: row.agency_name for row in db.query(AgentProfile.id, AgentProfile.agency_name).all()
    }
    return {
        "records": [
            {
                "record_kind": row["record_kind"],
                "record_id": row["record_id"],
                "classification": row["origin_classification"],
                "reference": row["reference"],
                "created_at": row["created_at"],
                "title": row["title"],
                "trip_id": row["trip_id"],
                "reporter_reference": row["reporter_reference"],
                "port_name": row["port_name"],
                "current_context": {
                    "vessel_id": row["vessel_id"],
                    "vessel_call_id": row["vessel_call_id"],
                    "agency_id": row["agency_id"],
                    "crew_assignment_id": row["crew_assignment_id"],
                    "port_id": row["port_id"],
                    "context_resolution": row["context_resolution"],
                },
            }
            for row in record_rows
        ],
        "record_total": record_total,
        "vessel_calls": [
            {
                "id": call.id,
                "vessel_id": call.vessel_id,
                "vessel_name": call.vessel_name,
                "imo_number": call.imo_number,
                "agency_id": call.agency_id,
                "agency_name": call.agency_name or agencies.get(call.agency_id),
                "port_name": call.port_name,
                "started_at": call.started_at,
                "ended_at": call.ended_at,
                "status": call.status,
            }
            for call in calls
        ],
        "vessel_call_total": vessel_call_total,
    }


@router.post("/historical-context/{record_kind}/{record_id}/reconcile")
def reconcile_historical_context(
    record_kind: str,
    record_id: int,
    body: HistoricalContextResolutionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Attach one legacy event to reviewed historical evidence, with an audit row."""
    verify_superadmin(current_user)
    from app.db.models.crew_assignment import CrewAssignment
    from app.db.models.crew_sos import CrewSos
    from app.db.models.event_context_reconciliation import EventContextReconciliation
    from app.db.models.vessel_call import VesselCall

    kind = record_kind.strip().lower()
    model = (
        Incident
        if kind == "incident"
        else CrewSos if kind == "sos" else CabBooking if kind == "booking" else None
    )
    if model is None:
        raise HTTPException(status_code=404, detail="Historical record not found")
    evidence_type = body.evidence_type.strip().lower()
    if evidence_type not in HISTORICAL_EVIDENCE_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported evidence type")

    record = db.query(model).filter(model.id == record_id).with_for_update().first()
    if record is None:
        raise HTTPException(status_code=404, detail="Historical record not found")
    locked_context = _context_dict(record)
    if body.expected_context is not None:
        expected = {
            key: body.expected_context.get(key)
            for key in locked_context
        }
        if expected != locked_context:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Historical record changed after it was loaded; refresh and review it again",
                    "current_context": locked_context,
                },
            )
    existing_call = db.query(VesselCall).filter(
        VesselCall.id == record.vessel_call_id
    ).first() if record.vessel_call_id else None
    if not _needs_context_reconciliation(record, existing_call):
        raise HTTPException(status_code=409, detail="Historical record is already resolved")

    call = db.query(VesselCall).filter(VesselCall.id == body.vessel_call_id).first()
    if call is None or call.vessel_id is None:
        raise HTTPException(
            status_code=422, detail="Selected vessel call has incomplete ownership"
        )

    booking = record if kind == "booking" else None
    if kind == "sos" and record.cab_booking_id:
        booking = (
            db.query(CabBooking).filter(CabBooking.id == record.cab_booking_id).first()
        )
    if booking is None and getattr(record, "trip_id", None):
        booking = (
            db.query(CabBooking).filter(CabBooking.booking_id == record.trip_id).first()
        )
    if booking is not None and booking.vessel_call_id is not None:
        if booking.vessel_call_id != call.id:
            raise HTTPException(
                status_code=409,
                detail="Selected vessel call conflicts with the linked booking",
            )
        if evidence_type != "trip_record":
            raise HTTPException(
                status_code=422,
                detail="A linked booking must be recorded as trip_record evidence",
            )

    previous = locked_context
    assignment = None
    if record.crew_assignment_id:
        assignment = db.query(CrewAssignment).filter(
            CrewAssignment.id == record.crew_assignment_id,
            CrewAssignment.vessel_call_id == call.id,
        ).first()
        if assignment is None:
            raise HTTPException(
                status_code=409,
                detail="Record crew assignment belongs to a different vessel call",
            )

    record.vessel_id = call.vessel_id
    record.vessel_call_id = call.id
    record.agency_id = call.agency_id
    record.port_id = call.port_id
    record.crew_assignment_id = assignment.id if assignment else None
    record.context_resolution = (
        "CONFIRMED_AGENCY" if call.agency_id is not None else "CONFIRMED_UNMANAGED"
    )
    resolved = _context_dict(record)
    db.add(
        EventContextReconciliation(
            record_kind=kind,
            record_id=record.id,
            previous_context=previous,
            resolved_context=resolved,
            evidence_type=evidence_type,
            evidence_reference=(body.evidence_reference or "").strip() or None,
            notes=body.notes.strip(),
            reconciled_by_user_id=current_user.id,
        )
    )
    db.commit()
    return {
        "record_kind": kind,
        "record_id": record.id,
        "context": resolved,
    }


@router.get("/historical-context/audit")
def list_historical_context_audit(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from app.db.models.event_context_reconciliation import EventContextReconciliation

    rows = db.query(EventContextReconciliation).order_by(
        EventContextReconciliation.created_at.desc(),
        EventContextReconciliation.id.desc(),
    ).limit(500).all()
    return [
        {
            "id": row.id,
            "record_kind": row.record_kind,
            "record_id": row.record_id,
            "previous_context": row.previous_context,
            "resolved_context": row.resolved_context,
            "evidence_type": row.evidence_type,
            "evidence_reference": row.evidence_reference,
            "notes": row.notes,
            "reconciled_by_user_id": row.reconciled_by_user_id,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/identity-conflicts/{conflict_id}/audit")
def identity_audit_page(
    conflict_id: int,
    offset: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from app.db.models.crew_identity_conflict import (
        CrewIdentityConflictRecord,
        CrewIdentityConflictAudit,
    )

    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid audit pagination")
    if db.get(CrewIdentityConflictRecord, conflict_id) is None:
        raise HTTPException(404, "Identity conflict not found")
    query = db.query(CrewIdentityConflictAudit).filter_by(conflict_id=conflict_id)
    rows = (
        query.order_by(
            CrewIdentityConflictAudit.created_at.desc(),
            CrewIdentityConflictAudit.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "action": r.action,
                "created_at": r.created_at,
                "reason": r.reason,
            }
            for r in rows
        ],
        "total": query.count(),
        "offset": offset,
        "limit": limit,
    }
