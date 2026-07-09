from fastapi import APIRouter, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.encoders import jsonable_encoder
import os
import shutil
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, cast, String
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel
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

router = APIRouter()
logger = logging.getLogger(__name__)

# --- Schemas ---

class DashboardStats(BaseModel):
    total_restaurants: int
    total_crew: int
    total_sightseeing: int
    total_pubs: int
    total_hotels: int
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
    other_information: Optional[Dict[str, Any]] = None
    

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
    other_information: Optional[Dict[str, Any]] = None
    port_id: Optional[int] = None
    status: Optional[str] = None



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
    other_information: Optional[Dict[str, Any]]
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
    
# --- Helpers ---

def verify_superadmin(current_user: User):
    if current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superadmins can access this resource"
        )


def slugify_tag(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


def ensure_vendor_tags_table(db: Session) -> None:
    # Lets existing environments start using tags without waiting for migrations.
    VendorTag.__table__.create(bind=db.get_bind(), checkfirst=True)

# --- Routes ---

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
    return query.all()

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
    return query.all()

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

    return query.all()

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
    
    return query.all()

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

    link_rows = link_query.order_by(DriverMagicLink.updated_at.desc(), DriverMagicLink.id.desc()).all()
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
def track_drivers(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    return db.query(Driver).all()

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

        providers = query.order_by(AggregatorProfile.company_name.asc()).all()
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
def track_incidents(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    return db.query(Incident).order_by(Incident.created_at.desc()).all()

@router.get("/tracking/crew")
def track_crew(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    from sqlalchemy.orm import joinedload
    return db.query(CrewProfile).options(joinedload(CrewProfile.user)).all()

@router.get("/tracking/service-requests")
def track_service_requests(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    return db.query(PortServiceRequest).order_by(PortServiceRequest.created_at.desc()).all()

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
    return query.order_by(ContactMessage.created_at.desc()).all()


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
    return query.order_by(VendorTag.sort_order.asc(), VendorTag.name.asc()).all()


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
    if raw_category not in {"restaurant", "pub", "hotel", "sightseeing"}:
        raise HTTPException(status_code=400, detail="Invalid category")

    port_id = data.get("port_id")
    if port_id is not None:
        if port_id <= 0:
            raise HTTPException(status_code=400, detail="port_id must be a valid port")
        port = db.query(Port).filter(Port.id == port_id).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {port_id} does not exist")

    data["category"] = raw_category
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
        query = query.filter(Vendors.category == category.strip().lower())

    if search is not None:
        query = query.filter(Vendors.name.ilike(f"%{search}%"))     

    return query.all()

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
        if patch["category"] not in {"restaurant", "pub", "hotel", "sightseeing"}:
            raise HTTPException(status_code=400, detail="Invalid category")
    if "phone" in patch and patch["phone"] is None:
        patch["phone"] = ""
    if "email" in patch and patch["email"] is None:
        patch["email"] = ""
    if "port_id" in patch and patch["port_id"] is not None:
        if patch["port_id"] <= 0:
            raise HTTPException(status_code=400, detail="port_id must be a valid port")
        port = db.query(Port).filter(Port.id == patch["port_id"]).first()
        if not port:
            raise HTTPException(status_code=400, detail=f"Port with ID {patch['port_id']} does not exist")

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
    agents = db.query(User).filter(User.role == "agent").all()
    out = []
    for u in agents:
        prof = db.query(AgentProfile).filter(AgentProfile.user_id == u.id).first()
        out.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "mobile_number": u.mobile_number,
            "agency_name": prof.agency_name if prof else "",
            "location": prof.location if prof else "",
            "agent_identifier": prof.agent_identifier if prof else "",
            "assigned_port": prof.assigned_port if prof else None,
            "license_number": prof.license_number if prof else None,
            "auth_document_url": prof.auth_document_url if prof else None
        })
    return out

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
    crew_count: Optional[int] = 0
    total_crew: Optional[int] = 0
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: Optional[str] = "Active"

from app.api.v1.routes_vessels import VesselOut

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

    c_count = body.crew_count if body.crew_count is not None else 0
    if body.total_crew is not None:
        c_count = body.total_crew

    vessel = Vessel(
        agent_id=agent.id,
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
        
    return db.query(Vessel).filter(Vessel.agent_id == agent.id).all()
