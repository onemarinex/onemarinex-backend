from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models.vendors import Vendors
from app.api.v1.routes_auth import get_current_user

router = APIRouter()


class FacilityOut(BaseModel):
    id: int
    name: str
    location_name: str
    distance_from_port: float
    rating: float
    price_per_person: float = 0.0
    timings: str = ""
    phone: Optional[str] = None
    lat: float
    lng: float
    image_url: List[str] = []
    description: str = ""
    about: str = ""
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    working_days: Optional[str] = None
    facilities: Optional[List[str]] = None
    best_for: Optional[str] = None
    category: Optional[str] = None

    class Config:
        from_attributes = True


def _vendor_to_facility(v: Vendors) -> FacilityOut:
    other = v.other_information or {}
    images = (
        v.images
        if v.images and len(v.images) > 0
        else [
            "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=400&q=80"
        ]
    )
    return FacilityOut(
        id=v.id,
        name=v.name,
        location_name=v.location_name,
        distance_from_port=v.distance_from_port,
        rating=v.rating,
        price_per_person=other.get("price_per_person", 0.0),
        timings=other.get("timings", ""),
        phone=v.phone,
        lat=v.lat,
        lng=v.lng,
        image_url=images,
        description=other.get("about") or other.get("description") or "",
        about=other.get("about") or other.get("description") or "",
        open_time=other.get("open_time"),
        close_time=other.get("close_time"),
        working_days=other.get("working_days"),
        facilities=other.get("facilities", []),
        best_for=other.get("best_for", ""),
        category=other.get("category", v.category),
    )


@router.get("/massage-wellness", response_model=List[FacilityOut])
def get_massage_wellness(
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = db.query(Vendors).filter(
        Vendors.status == "Active",
        Vendors.category.in_(["massage", "wellness"]),
    )
    if port_id:
        query = query.filter(Vendors.port_id == port_id)
    vendors = query.all()
    return [_vendor_to_facility(v) for v in vendors]


@router.get("/shopping-utility", response_model=List[FacilityOut])
def get_shopping_utility(
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = db.query(Vendors).filter(
        Vendors.status == "Active",
        Vendors.category.in_(["shopping", "utility"]),
    )
    if port_id:
        query = query.filter(Vendors.port_id == port_id)
    vendors = query.all()
    return [_vendor_to_facility(v) for v in vendors]


@router.get("/{facility_id}", response_model=FacilityOut)
def get_facility_by_id(
    facility_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    vendor = db.query(Vendors).filter(
        Vendors.id == facility_id,
        Vendors.status == "Active",
        Vendors.category.in_(["massage", "wellness", "shopping", "utility"]),
    ).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Facility not found")
    return _vendor_to_facility(vendor)
