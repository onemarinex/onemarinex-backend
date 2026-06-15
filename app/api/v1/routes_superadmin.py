from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel

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
from app.db.models.driver_magic_link import DriverMagicLink
from app.api.v1.routes_auth import get_current_user

router = APIRouter()

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
    port_id: int

class VendorCreate(VendorCreationBase):
    # Optional at creation
    rating: Optional[float] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    documents: Optional[List[str]] = None
    images: Optional[List[str]] = None
    other_information: Optional[Dict[str, Any]] = None
    

class VendorUpdate(BaseModel):
    name: Optional[str]
    category: Optional[str]
    location_name: Optional[str]
    distance_from_port: Optional[float]
    lat: Optional[float]
    lng: Optional[float]
    rating: Optional[float]
    phone: Optional[str]
    email: Optional[str]
    documents: Optional[List[str]]
    images: Optional[List[str]]
    other_information: Optional[Dict[str, Any]]
    port_id: Optional[int]
    status: Optional[str]



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
    
# --- Helpers ---

def verify_superadmin(current_user: User):
    if current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superadmins can access this resource"
        )

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
    row.category.value: row.count for row in results
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
    from sqlalchemy.orm import joinedload
    from app.services.booking_service import serialize_booking

    query = db.query(CabBooking).options(
        joinedload(CabBooking.crew),
        joinedload(CabBooking.assigned_driver),
        joinedload(CabBooking.provider),
    )
    if status:
        try:
            query = query.filter(CabBooking.status == BookingStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
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
        query = query.join(
            AggregatorProfile,
            or_(
                CabBooking.provider_id == AggregatorProfile.id,
                CabBooking.aggregator_id == AggregatorProfile.id,
            ),
        ).filter(AggregatorProfile.provider_type == provider_type)
    if date_from:
        query = query.filter(CabBooking.created_at >= date_from)
    if date_to:
        query = query.filter(CabBooking.created_at <= date_to)

    bookings = query.order_by(CabBooking.created_at.desc()).all()
    return [serialize_booking(booking) for booking in bookings]


@router.get("/tracking/magic-links")
def track_magic_links(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    from sqlalchemy.orm import joinedload

    query = db.query(DriverMagicLink).options(
        joinedload(DriverMagicLink.booking).joinedload(CabBooking.crew),
        joinedload(DriverMagicLink.booking).joinedload(CabBooking.assigned_driver),
        joinedload(DriverMagicLink.booking).joinedload(CabBooking.aggregator),
        joinedload(DriverMagicLink.reach_events),
    )

    if search:
        pattern = f"%{search}%"
        query = query.join(CabBooking, DriverMagicLink.booking_id == CabBooking.id).join(
            CrewProfile,
            CabBooking.crew_id == CrewProfile.id,
        ).outerjoin(
            Driver,
            CabBooking.assigned_driver_id == Driver.id,
        ).outerjoin(
            AggregatorProfile,
            or_(CabBooking.provider_id == AggregatorProfile.id, CabBooking.aggregator_id == AggregatorProfile.id),
        ).filter(
            or_(
                CrewProfile.full_name.ilike(pattern),
                Driver.name.ilike(pattern),
                AggregatorProfile.company_name.ilike(pattern),
            )
        )

    links = query.order_by(DriverMagicLink.updated_at.desc(), DriverMagicLink.id.desc()).all()
    response: List[Dict[str, Any]] = []
    for link in links:
        booking = link.booking
        if not booking:
            continue
        reached_count = len({event.stop_id for event in (link.reach_events or [])})
        itinerary_count = len(link.itinerary_stops or [])
        latest_event = (link.reach_events or [None])[0]
        response.append(
            {
                "id": link.id,
                "token": link.token,
                "magic_path": f"/magic-link/{link.token}",
                "booking_id": booking.booking_id,
                "aggregator_name": booking.aggregator_name,
                "driver_name": booking.driver_name,
                "crew_name": booking.crew.full_name if booking.crew else None,
                "crew_hpid": booking.crew.hpid if booking.crew else None,
                "pickup_address": booking.pickup_address,
                "drop_address": booking.drop_address,
                "reached_count": reached_count,
                "itinerary_count": itinerary_count,
                "latest_reached_at": latest_event.reached_at if latest_event else None,
                "latest_reached_latitude": latest_event.latitude if latest_event else None,
                "latest_reached_longitude": latest_event.longitude if latest_event else None,
                "created_at": link.created_at,
                "updated_at": link.updated_at,
                "itinerary": link.itinerary_stops or [],
                "events": [
                    {
                        "id": event.id,
                        "stop_id": event.stop_id,
                        "stop_name": event.stop_name,
                        "latitude": event.latitude,
                        "longitude": event.longitude,
                        "notes": event.notes,
                        "reached_at": event.reached_at,
                    }
                    for event in (link.reach_events or [])
                ],
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
            BookingStatus.PENDING_PROVIDER_RESPONSE,
            BookingStatus.PROVIDER_ACCEPTED,
            BookingStatus.DRIVER_ASSIGNED,
            BookingStatus.DRIVER_ACCEPTED,
            BookingStatus.ON_TRIP,
            BookingStatus.PENDING,
            BookingStatus.CONFIRMED,
            BookingStatus.ARRIVED,
            BookingStatus.IN_PROGRESS,
        ]
        active_booking_counts: Dict[int, int] = {}
        completed_trip_counts: Dict[int, int] = {}
        if provider_ids:
            active_booking_counts = dict(
                db.query(CabBooking.aggregator_id, func.count(CabBooking.id))
                .filter(
                    CabBooking.aggregator_id.in_(provider_ids),
                    CabBooking.status.in_(active_statuses),
                )
                .group_by(CabBooking.aggregator_id)
                .all()
            )
            completed_trip_counts = dict(
                db.query(CabBooking.aggregator_id, func.count(CabBooking.id))
                .filter(
                    CabBooking.aggregator_id.in_(provider_ids),
                    CabBooking.status == BookingStatus.COMPLETED,
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
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load aggregator tracking data: {e}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load aggregator tracking data: {exc}")
        
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


@router.post("/vendors", response_model=VendorOut)
def create_place(payload: VendorCreate, db: Session = Depends(get_db),current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    vendor = Vendors(**payload.dict())
    
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
):
    from sqlalchemy.orm import joinedload

    query = db.query(Vendors).options(joinedload(Vendors.port))
    if port_id is not None:
        query = query.filter(Vendors.port_id == port_id)

    if vendor_id is not None:
        query = query.filter(Vendors.id == vendor_id)

    if category is not None:
        query = query.filter(Vendors.category == category)

    if search is not None:
        query = query.filter(Vendors.name.ilike(f"%{search}%"))     

    return query.all()

@router.put("/vendors/{vendor_id}", response_model=VendorOut)
def update_place(vendor_id: int, payload: VendorUpdate, db: Session = Depends(get_db)):

    vendor = db.query(Vendors).filter(Vendors.id == vendor_id).first()

    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    for key, value in payload.dict(exclude_unset=True).items():
        setattr(vendor, key, value)

    db.commit()
    db.refresh(vendor)

    return vendor
