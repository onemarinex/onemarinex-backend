from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.restaurant import Restaurant
from app.db.models.hotels import Hotel
from app.db.models.pub import Pub
from app.db.models.sightseeing import Sightseeing
from app.db.models.crew_profile import CrewProfile
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.driver import Driver
from app.db.models.port import Port
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.incident import Incident
from app.db.models.port_service_request import PortServiceRequest
from app.api.v1.routes_auth import get_current_user

router = APIRouter()

# --- Schemas ---

class DashboardStats(BaseModel):
    total_restaurants: int
    total_crew: int
    total_sightseeing: int
    total_pubs: int
    total_hotels: int

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

class SightseeingCreate(VendorBase):
    location_name: str
    distance_from_port: float
    price_per_person: float = 0
    timings: Optional[str] = None
    address: Optional[str] = None
    images: Optional[List[str]] = None

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
    
    query_res = db.query(Restaurant)
    query_hotel = db.query(Hotel)
    query_pub = db.query(Pub)
    query_sight = db.query(Sightseeing)
    query_crew = db.query(CrewProfile)
    
    if port_id:
        # Get port object to use port name for crew/incident filtering if needed
        port_obj = db.query(Port).filter(Port.id == port_id).first()
        port_name = port_obj.name if port_obj else None
        
        query_res = query_res.filter(Restaurant.port_id == port_id)
        query_hotel = query_hotel.filter(Hotel.port_id == port_id)
        query_pub = query_pub.filter(Pub.port_id == port_id)
        query_sight = query_sight.filter(Sightseeing.port_id == port_id)
        
        if port_name:
            # Filtering crew and incidents by port name logic
            query_crew = query_crew.filter(CrewProfile.current_port.ilike(f"%{port_name}%"))
    
    return DashboardStats(
        total_restaurants=query_res.count(),
        total_crew=query_crew.count(),
        total_sightseeing=query_sight.count(),
        total_pubs=query_pub.count(),
        total_hotels=query_hotel.count()
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
def track_cab_bookings(status: Optional[str] = None, port_id: Optional[int] = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    from sqlalchemy.orm import joinedload
    query = db.query(CabBooking).options(joinedload(CabBooking.crew), joinedload(CabBooking.assigned_driver))
    if status:
        query = query.filter(CabBooking.status == status)
    if port_id:
        port_obj = db.query(Port).filter(Port.id == port_id).first()
        if port_obj:
            query = query.join(CrewProfile, CabBooking.crew_profile_id == CrewProfile.id)
            query = query.filter(CrewProfile.current_port.ilike(f"%{port_obj.name}%"))
    return query.order_by(CabBooking.created_at.desc()).all()

@router.get("/tracking/drivers")
def track_drivers(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    return db.query(Driver).all()

@router.get("/tracking/aggregators")
def track_aggregators(port_id: Optional[int] = None,search : Optional[str] = None ,db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    verify_superadmin(current_user)
    from sqlalchemy.orm import joinedload
    query = db.query(AggregatorProfile).options(joinedload(AggregatorProfile.user),joinedload(AggregatorProfile.operating_port))
    if port_id:
        query = query.filter(AggregatorProfile.operating_port_id == port_id)
    if search is not None:
        query = query.filter(AggregatorProfile.company_name.ilike(f"%{search}%")) 
    return query.all()
        
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
